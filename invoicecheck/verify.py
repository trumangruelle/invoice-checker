"""Verification: every invoice gets PASS or NEEDS REVIEW, with specific reasons.

The checks do not trust the extractor. They recompute what the invoice
claims (line totals, subtotal, tax, grand total), look for the same invoice
twice, sanity-check the dates, and, for AI extractions, confirm that each
number the model returned is actually printed in the source document.
"""

from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .model import Invoice
from .textutil import CENT, alnum_upper, cents, fmt_money, fmt_number, fmt_price, numbers_in

PASS = "PASS"
NEEDS_REVIEW = "NEEDS REVIEW"
APPROVED = "APPROVED"         # held, then approved by a named person; see review.py
NOT_CHECKED = "NOT CHECKED"   # a file in the folder that could not be read at all

ZERO = Decimal("0")

# Check codes and what each one means, in the order they are run.
CHECKS = {
    "extraction": "the extractor recognised the layout and read every item row",
    "required_fields": "vendor, invoice number, invoice date, at least one line item and a total are present",
    "line_incomplete": "every line item has an amount, and a quantity and unit price either both or neither",
    "line_math": "quantity x unit price = line amount, within a cent (when both are printed)",
    "lines_sum": "the line amounts add up to the subtotal, within a cent (to total minus tax when no subtotal is printed)",
    "tax_rate": "when a tax rate is printed, tax = rate x subtotal (a cent of rounding allowed per two lines)",
    "total_math": "subtotal + tax = total, within a cent",
    "total_not_positive": "the total is above zero (credit notes and refunds are left to a person)",
    "date_future": "the invoice date is not after the run date",
    "date_stale": "the invoice date is not older than --max-age-days (365 by default; 0 turns this off)",
    "date_unreadable": "a printed due date can be read as a date",
    "due_before_issue": "the due date is not before the invoice date",
    "duplicate_number": "no other file in the batch has the same vendor and invoice number",
    "possible_duplicate": "no other file has the same vendor, date and total under a different invoice number, "
                          "or the same invoice number with the same total or date under a different vendor name",
    "already_seen": "the vendor and invoice number are not in the --history file of earlier runs",
    "not_in_source": "(AI extraction only) every amount and the invoice number appear in the source document",
}

# The same checks in a few plain words, for the console summary and the reports.
LABELS = {
    "extraction": "not fully read",
    "required_fields": "details missing",
    "line_incomplete": "line incomplete",
    "line_math": "line total is wrong",
    "lines_sum": "items don't add up",
    "tax_rate": "tax doesn't match its rate",
    "total_math": "total is wrong",
    "total_not_positive": "zero or negative total",
    "date_future": "date in the future",
    "date_stale": "very old invoice",
    "date_unreadable": "due date unreadable",
    "due_before_issue": "due before it was issued",
    "duplicate_number": "sent twice",
    "possible_duplicate": "may be a duplicate",
    "already_seen": "seen in an earlier run",
    "not_in_source": "number not on the page",
}

# Checks whose difference is money the invoice asks for beyond what its own figures support.
ARITHMETIC_CHECKS = ("line_math", "lines_sum", "tax_rate", "total_math")
# Findings that mean the figures themselves may not be what is printed.
UNTRUSTED_FIGURES = ("extraction", "required_fields", "not_in_source")

_VENDOR_SUFFIXES = re.compile(
    r"\b(?:llc|l\.l\.c|inc|incorporated|co|company|corp|corporation|ltd|limited|plc)\b\.?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    check: str
    message: str
    # Money this finding says the invoice asks for beyond what its own figures support.
    # Negative when the invoice asks for less. None when the finding is not about an amount.
    overcharge: Decimal | None = None
    # Set only on a repeat copy of an invoice (duplicate_number on the later file, or
    # already_seen): its whole total, which paying this copy too would pay twice. It is
    # reported as "sent twice", apart from overbilling: the invoice is real, it came twice.
    repeat_total: Decimal | None = None


@dataclass
class Verdict:
    invoice: Invoice
    findings: list[Finding] = field(default_factory=list)
    # Set by review.apply_approvals; a review.Approval that covers this invoice, or None.
    # Typed loosely so verify.py stays free of review.py (review.py imports this module).
    approval: Any = None
    # Why an approval on file was *not* applied, in plain words. Printed and written to
    # every report: an approval that stopped working must never fail silently.
    approval_note: str | None = None

    @property
    def status(self) -> str:
        if not self.findings:
            return PASS
        return APPROVED if self.approval is not None else NEEDS_REVIEW

    @property
    def importable(self) -> bool:
        """Whether this invoice goes into the import file: it passed, or a person approved it."""
        return self.status in (PASS, APPROVED)

    @property
    def is_repeat_copy(self) -> bool:
        return any(f.repeat_total is not None for f in self.findings)

    @property
    def repeat_total(self) -> Decimal:
        """For a repeat copy of an invoice, the whole total: what paying this copy too would pay twice.

        A repeat copy is the second file with the same vendor and number, or an invoice already
        in the history file. The first copy is zero, because paying it is paying the invoice.
        This is never added to the overbilling figure.
        """
        copies = [f.repeat_total for f in self.findings if f.repeat_total is not None]
        return max(copies) if copies else ZERO

    @property
    def overcharge(self) -> Decimal:
        """Overbilling the arithmetic can put a number on; zero when it cannot.

        The net of the arithmetic differences, counted only when it is in the vendor's
        favour and only when the invoice was fully read. A repeat copy counts nothing here:
        its figures are the original's, which are counted once, on the original.
        """
        if self.is_repeat_copy:
            return ZERO
        if any(f.check in UNTRUSTED_FIGURES for f in self.findings):
            return ZERO
        net = sum((f.overcharge for f in self.findings
                   if f.check in ARITHMETIC_CHECKS and f.overcharge is not None), ZERO)
        return max(net, ZERO)


@dataclass(frozen=True)
class MoneySummary:
    held_invoices: int
    held_total: Decimal       # the printed totals of every invoice held for review
    overcharge: Decimal       # arithmetic differences in the vendor's favour; duplicates are not in it
    repeat_copies: int        # held files that are a repeat copy of an invoice (sent twice)
    repeat_total: Decimal     # what paying those copies too would pay a second time
    approved_invoices: int    # invoices a person approved out of the review queue
    approved_total: Decimal   # the printed totals of those invoices
    approved_overcharge: Decimal   # overbilling that went into the import file anyway, with a name on it


def money_summary(verdicts: list[Verdict]) -> MoneySummary:
    held = [v for v in verdicts if v.status == NEEDS_REVIEW]
    overcharge = sum((v.overcharge for v in held), ZERO)
    copies = [v for v in held if v.is_repeat_copy]
    repeat_total = sum((v.repeat_total for v in copies), ZERO)
    held_total = sum((v.invoice.total for v in held if v.invoice.total is not None), ZERO)
    # An approved invoice is no longer held, so it is out of the figures above. Its own
    # overbilling is reported separately rather than dropped: a person let that money
    # through, and the report says how much and who.
    approved = [v for v in verdicts if v.status == APPROVED]
    approved_total = sum((v.invoice.total for v in approved if v.invoice.total is not None), ZERO)
    approved_overcharge = sum((v.overcharge for v in approved), ZERO)
    return MoneySummary(len(held), held_total, overcharge, len(copies), repeat_total,
                        len(approved), approved_total, approved_overcharge)


def vendor_key(name: str | None) -> str:
    """Vendor name for matching: case, punctuation and company suffixes ignored."""
    return alnum_upper(_VENDOR_SUFFIXES.sub(" ", name or ""))


def invoice_key(inv: Invoice) -> tuple[str, str] | None:
    number = alnum_upper(inv.invoice_number)
    if not number:
        return None
    return vendor_key(inv.vendor), number


def _short(text: str, limit: int = 40) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------
# Single-invoice checks
# --------------------------------------------------------------------------

def check_extraction(inv: Invoice) -> list[Finding]:
    return [Finding("extraction", f"Extractor note: {w}.") for w in inv.warnings]


def check_required(inv: Invoice) -> list[Finding]:
    out = []
    if not inv.vendor:
        out.append(Finding("required_fields", "No vendor name found."))
    if not inv.invoice_number:
        out.append(Finding("required_fields", "No invoice number found."))
    if inv.invoice_date is None:
        if inv.invoice_date_raw:
            out.append(Finding("required_fields",
                               f"Invoice date {inv.invoice_date_raw!r} is not a date I can read."))
        else:
            out.append(Finding("required_fields", "No invoice date found."))
    if not inv.line_items:
        out.append(Finding("required_fields", "No line items found."))
    if inv.total is None:
        out.append(Finding("required_fields", "No total found."))
    return out


def check_lines(inv: Invoice) -> list[Finding]:
    out = []
    for n, item in enumerate(inv.line_items, start=1):
        label = f"Item {n} ({_short(item.description)!r})"
        if item.amount is None:
            out.append(Finding("line_incomplete", f"{label} has no amount."))
            continue
        if item.quantity is None and item.unit_price is None:
            continue  # a lump-sum line: only the amount is printed
        if item.quantity is None or item.unit_price is None:
            has, lacks = ("quantity", "unit price") if item.unit_price is None else ("unit price", "quantity")
            out.append(Finding("line_incomplete", f"{label} has a {has} but no {lacks}."))
            continue
        expected = cents(item.quantity * item.unit_price)
        if abs(item.amount - expected) > CENT:
            out.append(Finding(
                "line_math",
                f"{label}: {fmt_number(item.quantity)} x {fmt_price(item.unit_price)} = "
                f"{fmt_money(expected)}, but the line shows {fmt_money(item.amount)} "
                f"(off by {fmt_money(abs(item.amount - expected))}).",
                item.amount - expected,
            ))
    return out


def check_lines_sum(inv: Invoice) -> list[Finding]:
    amounts = [item.amount for item in inv.line_items]
    if not amounts or any(a is None for a in amounts):
        return []
    lines = sum(amounts, Decimal("0"))
    if inv.subtotal is not None:
        if abs(lines - inv.subtotal) > CENT:
            return [Finding(
                "lines_sum",
                f"Line items add up to {fmt_money(lines)} but the subtotal says "
                f"{fmt_money(inv.subtotal)} (off by {fmt_money(abs(lines - inv.subtotal))}).",
                inv.subtotal - lines,
            )]
        return []
    if inv.total is None:
        return []
    # No subtotal printed: the lines plus any tax must make the total.
    tax = inv.tax if inv.tax is not None else Decimal("0")
    if abs(lines + tax - inv.total) > CENT:
        with_tax = f" plus tax {fmt_money(inv.tax)}" if inv.tax is not None else ""
        return [Finding(
            "lines_sum",
            f"Line items add up to {fmt_money(lines)}{with_tax} = {fmt_money(lines + tax)}, but the "
            f"total says {fmt_money(inv.total)} (off by {fmt_money(abs(lines + tax - inv.total))}); "
            f"no subtotal is printed.",
            inv.total - (lines + tax),
        )]
    return []


def check_tax_rate(inv: Invoice) -> list[Finding]:
    if inv.tax_rate is None or inv.tax is None or inv.subtotal is None:
        return []
    expected = cents(inv.subtotal * inv.tax_rate / 100)
    # Tax computed per line and then summed can drift by half a cent per line.
    tolerance = CENT * max(1, math.ceil(len(inv.line_items) / 2))
    if abs(inv.tax - expected) > tolerance:
        return [Finding(
            "tax_rate",
            f"Tax is {fmt_money(inv.tax)}, but {fmt_number(inv.tax_rate)}% of the "
            f"{fmt_money(inv.subtotal)} subtotal is {fmt_money(expected)} "
            f"(off by {fmt_money(abs(inv.tax - expected))}).",
            inv.tax - expected,
        )]
    return []


def check_total(inv: Invoice) -> list[Finding]:
    if inv.subtotal is None or inv.total is None:
        return []
    tax = inv.tax if inv.tax is not None else Decimal("0")
    expected = inv.subtotal + tax
    if abs(inv.total - expected) > CENT:
        if inv.tax is None:
            sum_text = f"Subtotal {fmt_money(inv.subtotal)} with no tax line"
        else:
            sum_text = (f"Subtotal {fmt_money(inv.subtotal)} + tax {fmt_money(inv.tax)} = "
                        f"{fmt_money(expected)}")
        return [Finding(
            "total_math",
            f"{sum_text}, but the total says {fmt_money(inv.total)} "
            f"(off by {fmt_money(abs(inv.total - expected))}).",
            inv.total - expected,
        )]
    return []


def check_total_positive(inv: Invoice) -> list[Finding]:
    if inv.total is not None and inv.total <= 0:
        return [Finding(
            "total_not_positive",
            f"Total is {fmt_money(inv.total)}; zero or negative totals (credit notes, refunds) "
            f"need manual handling.",
        )]
    return []


def check_dates(inv: Invoice, today: date, max_age_days: int) -> list[Finding]:
    out = []
    issued = inv.invoice_date
    if issued is not None:
        if issued > today:
            out.append(Finding(
                "date_future",
                f"Invoice date {issued.isoformat()} is in the future (checked against "
                f"{today.isoformat()}); check the year for a typo.",
            ))
        elif max_age_days > 0 and (today - issued).days > max_age_days:
            out.append(Finding(
                "date_stale",
                f"Invoice date {issued.isoformat()} is {(today - issued).days} days old "
                f"(limit {max_age_days}).",
            ))
    if inv.due_date is None and inv.due_date_raw:
        out.append(Finding("date_unreadable", f"Due date {inv.due_date_raw!r} is not a date I can read."))
    if issued is not None and inv.due_date is not None and inv.due_date < issued:
        out.append(Finding(
            "due_before_issue",
            f"Due date {inv.due_date.isoformat()} is before the invoice date {issued.isoformat()}.",
        ))
    return out


def check_grounding(inv: Invoice, source_text: str) -> list[Finding]:
    """For AI extractions: every amount and the invoice number must be printed in the source.

    A language model can produce numbers that are internally consistent and
    still not on the page. Arithmetic checks cannot catch that; this can.
    """
    printed = numbers_in(source_text)
    out = []

    def require(label: str, value: Decimal | None) -> None:
        if value is not None and value not in printed and -value not in printed:
            out.append(Finding("not_in_source",
                               f"AI returned {label} {fmt_number(value)}, which does not appear in the document."))

    require("subtotal", inv.subtotal)
    require("tax", inv.tax)
    require("total", inv.total)
    for n, item in enumerate(inv.line_items, start=1):
        require(f"item {n} quantity", item.quantity)
        require(f"item {n} unit price", item.unit_price)
        require(f"item {n} amount", item.amount)
    if inv.invoice_number and alnum_upper(inv.invoice_number) not in alnum_upper(source_text):
        out.append(Finding("not_in_source",
                           f"AI returned invoice number {inv.invoice_number!r}, which does not appear in the document."))
    return out


def verify_invoice(inv: Invoice, today: date, max_age_days: int = 365,
                   source_text: str | None = None) -> list[Finding]:
    findings = []
    findings += check_extraction(inv)
    findings += check_required(inv)
    findings += check_lines(inv)
    findings += check_lines_sum(inv)
    findings += check_tax_rate(inv)
    findings += check_total(inv)
    findings += check_total_positive(inv)
    findings += check_dates(inv, today, max_age_days)
    if inv.extractor.startswith("ai") and source_text is not None:
        findings += check_grounding(inv, source_text)
    return findings


# --------------------------------------------------------------------------
# Cross-file checks
# --------------------------------------------------------------------------

def duplicate_findings(invoices: list[Invoice]) -> dict[int, list[Finding]]:
    out: dict[int, list[Finding]] = defaultdict(list)

    by_number: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, inv in enumerate(invoices):
        key = invoice_key(inv)
        if key is not None:
            by_number[key].append(i)
    for indexes in by_number.values():
        if len(indexes) < 2:
            continue
        for position, i in enumerate(indexes):
            others = ", ".join(invoices[j].source_file for j in indexes if j != i)
            inv = invoices[i]
            # Every copy is held, but only the later ones are repeat copies: paying the first
            # is paying the invoice, paying another copy is paying it again.
            repeat = position > 0 and inv.total is not None and inv.total > 0
            out[i].append(Finding(
                "duplicate_number",
                f"Invoice number {inv.invoice_number} from {inv.vendor or 'this vendor'} also appears "
                f"in {others}; paying both would pay it twice.",
                repeat_total=inv.total if repeat else None,
            ))

    by_amount: dict[tuple[str, date, Decimal], list[int]] = defaultdict(list)
    for i, inv in enumerate(invoices):
        if inv.vendor and inv.invoice_date and inv.total is not None:
            by_amount[(vendor_key(inv.vendor), inv.invoice_date, inv.total)].append(i)
    for indexes in by_amount.values():
        for i in indexes:
            inv = invoices[i]
            others = [invoices[j] for j in indexes
                      if j != i and alnum_upper(invoices[j].invoice_number) != alnum_upper(inv.invoice_number)]
            if others:
                listed = ", ".join(f"{o.source_file} (invoice {o.invoice_number or 'unnumbered'})" for o in others)
                out[i].append(Finding(
                    "possible_duplicate",
                    f"Same vendor, date and total ({fmt_money(inv.total)} on {inv.invoice_date.isoformat()}) "
                    f"as {listed} under a different invoice number.",
                ))

    # The same invoice number under two vendor names, with the same total or date. This is
    # usually one invoice whose vendor was read two ways (a banner line, a trading name),
    # and the vendor-keyed checks above cannot see it.
    by_bare_number: dict[str, list[int]] = defaultdict(list)
    for i, inv in enumerate(invoices):
        number = alnum_upper(inv.invoice_number)
        if number:
            by_bare_number[number].append(i)
    for indexes in by_bare_number.values():
        for i in indexes:
            inv = invoices[i]
            listed = []
            for j in indexes:
                other = invoices[j]
                if j == i or vendor_key(other.vendor) == vendor_key(inv.vendor):
                    continue
                same = []
                if inv.total is not None and other.total == inv.total:
                    same.append("total")
                if inv.invoice_date is not None and other.invoice_date == inv.invoice_date:
                    same.append("date")
                if same:
                    listed.append(f"{other.source_file} (vendor {other.vendor or '(not found)'!r}, "
                                  f"same {' and '.join(same)})")
            if listed:
                out[i].append(Finding(
                    "possible_duplicate",
                    f"Invoice number {inv.invoice_number} also appears in {', '.join(listed)} under a "
                    f"different vendor name; it may be the same invoice with the vendor read differently.",
                ))
    return out


class HistoryError(Exception):
    """The --history CSV has no invoice numbers in it, so it would flag nothing."""


def load_history(path: Path) -> set[tuple[str, str]]:
    """Vendor + invoice number pairs from an earlier run's CSV (import_ready.csv or all_invoices.csv).

    A CSV without an InvoiceNumber column is an error, not an empty history.
    That happens when the file handed in is an import file whose columns were
    renamed for an accounting package (see --import-map), and a history that
    matches nothing would quietly stop catching invoices that came back.
    """
    seen = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "InvoiceNumber" not in reader.fieldnames:
            found = ", ".join(reader.fieldnames or []) or "(the file is empty)"
            raise HistoryError(f"it has no 'InvoiceNumber' column, so it would flag nothing. "
                               f"Its columns are: {found}. Use an import_ready.csv or "
                               f"all_invoices.csv written by this tool.")
        for row in reader:
            number = alnum_upper(row.get("InvoiceNumber"))
            if number:
                seen.add((vendor_key(row.get("Vendor")), number))
    return seen


def verify_all(invoices: list[Invoice], today: date, max_age_days: int = 365,
               sources: dict[str, str] | None = None,
               history: set[tuple[str, str]] | None = None) -> list[Verdict]:
    sources = sources or {}
    verdicts = [
        Verdict(inv, verify_invoice(inv, today, max_age_days, sources.get(inv.source_file)))
        for inv in invoices
    ]
    for i, findings in duplicate_findings(invoices).items():
        verdicts[i].findings.extend(findings)
    if history:
        for verdict in verdicts:
            key = invoice_key(verdict.invoice)
            if key is not None and key in history:
                inv = verdict.invoice
                verdict.findings.append(Finding(
                    "already_seen",
                    f"Invoice {inv.invoice_number} from {inv.vendor or 'this vendor'} is already in the "
                    f"history file from an earlier run; it may have been paid.",
                    repeat_total=inv.total if inv.total is not None and inv.total > 0 else None,
                ))
    return verdicts
