"""Writing the results: an import CSV, a summary CSV, a review report (Markdown and HTML) and JSON."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path
from typing import Sequence

from .documents import Unread
from .importmap import ImportMapping, map_row
from .review import invoice_hash
from .textutil import fmt_money, fmt_number, money_str, price_str
from .verify import (APPROVED, CHECKS, LABELS, NEEDS_REVIEW, NOT_CHECKED, PASS, MoneySummary, Verdict,
                     money_summary)

IMPORT_COLUMNS = [
    "Vendor", "InvoiceNumber", "InvoiceDate", "DueDate", "Currency", "LineNumber",
    "Description", "Quantity", "UnitPrice", "LineAmount", "InvoiceSubtotal",
    "InvoiceTax", "InvoiceTotal", "SourceFile",
]

SUMMARY_COLUMNS = [
    "SourceFile", "Status", "Vendor", "InvoiceNumber", "InvoiceDate", "DueDate",
    "Subtotal", "Tax", "Total", "Overbilled", "SentTwiceAmount", "LineCount", "Layout", "Extractor",
    "ApprovedBy", "ApprovedAt", "Reasons",
]

# One row per flag, not per invoice: an invoice held for three reasons gets three rows,
# each with its own plain-English reason and its own money, so a person can sort or
# filter by reason and none of them hides inside a cell with the other two.
EXCEPTION_COLUMNS = [
    "SourceFile", "Status", "Vendor", "InvoiceNumber", "InvoiceDate", "InvoiceTotal",
    "Check", "Flag", "Reason", "FindingDifference", "DifferenceKind",
    "InvoiceOverbilled", "InvoiceSentTwice", "ApprovedBy", "ApprovedAt", "ApprovedReason",
    "QueueNote",
]

# The pseudo-check used for a file that could not be read at all. It is not one of the
# CHECKS (nothing in it was checked), but it belongs on the exceptions list: it is a
# file that did not reach the import file, with a reason.
NOT_CHECKED_CHECK = "not_checked"
NOT_CHECKED_LABEL = "not checked"

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: str | None) -> str:
    """Neutralise spreadsheet formulas in free-text cells ("=HYPERLINK(...)" and friends).

    Excel, LibreOffice and Google Sheets all treat a cell that opens with "=",
    "+", "-" or "@" as a formula, so a vendor name or a description copied off
    a hostile invoice can run when the file is opened. A leading apostrophe
    forces the cell to text. Applied to every free-text cell in every CSV.

    Amounts are deliberately *not* put through this: they come from money_str
    and are always plain numbers ("-15.00"), which a spreadsheet must read as
    numbers for the file to be worth importing.
    """
    value = value or ""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def _label(check: str) -> str:
    text = LABELS.get(check, check)
    return text[:1].upper() + text[1:]


def result_line(verdicts: Sequence[Verdict], unread: Sequence[Unread]) -> str:
    passed = sum(v.status == PASS for v in verdicts)
    approved = sum(v.status == APPROVED for v in verdicts)
    review = len(verdicts) - passed - approved
    # The APPROVED part appears only when the review queue is in use, the way the
    # "Sent twice" line appears only when something was sent twice.
    middle = f"{approved} {APPROVED}, " if approved else ""
    return f"{passed} PASS, {middle}{review} {NEEDS_REVIEW}, {len(unread)} {NOT_CHECKED}"


def import_contents_line(money: MoneySummary, *, tick: str = "") -> str:
    """What is in the import file, said in one line wherever it is written.

    `tick` is the Markdown backtick around the file name; the HTML report and
    the console pass nothing.
    """
    name = f"{tick}import_ready.csv{tick}"
    if not money.approved_invoices:
        return f"Only PASS invoices are in {name}."
    noun = "invoice" if money.approved_invoices == 1 else "invoices"
    return (f"{name} holds the PASS invoices and the {money.approved_invoices} held "
            f"{noun} a person approved.")


def approved_line(money: MoneySummary) -> str | None:
    """The invoices a person let through the review queue, and the overbilling among them."""
    if not money.approved_invoices:
        return None
    noun = "invoice" if money.approved_invoices == 1 else "invoices"
    extra = (f", including {fmt_money(money.approved_overcharge)} of overbilling"
             if money.approved_overcharge else "")
    return (f"Approved for import: {money.approved_invoices} {noun} totalling "
            f"{fmt_money(money.approved_total)}{extra}")


def overbilling_line(money: MoneySummary) -> str:
    if not money.overcharge:
        return "Overbilling found: $0.00"
    return f"Overbilling found: {fmt_money(money.overcharge)} in arithmetic errors"


def sent_twice_line(money: MoneySummary) -> str | None:
    """The invoices sent twice, apart from overbilling: the invoice is real, it just must not be paid twice."""
    if not money.repeat_copies:
        return None
    if money.repeat_copies == 1:
        return f"Sent twice: a {fmt_money(money.repeat_total)} invoice, held so it is not paid twice"
    return (f"Sent twice: {money.repeat_copies} invoices totalling {fmt_money(money.repeat_total)}, "
            f"held so none is paid twice")


def _sent_twice_tile_label(money: MoneySummary) -> str:
    if not money.repeat_copies:
        return "no invoice sent twice"
    if money.repeat_copies == 1:
        return "1 invoice sent twice, held so it is not paid twice"
    return f"{money.repeat_copies} invoices sent twice, held so none is paid twice"


def held_line(money: MoneySummary) -> str:
    noun = "invoice" if money.held_invoices == 1 else "invoices"
    return f"Held for review: {money.held_invoices} {noun} totalling {fmt_money(money.held_total)}"


def _overbilled_note(verdict: Verdict) -> str | None:
    return fmt_money(verdict.overcharge) if verdict.overcharge else None


def _second_copy_note(verdict: Verdict) -> str | None:
    return f"Second copy of a {fmt_money(verdict.repeat_total)} invoice" if verdict.repeat_total else None


@dataclass(frozen=True)
class ImportRows:
    """The import file's contents: one row per line item, plus how they were let through."""
    rows: list[list]
    passed: int      # invoices that passed every check
    approved: int    # invoices a person approved out of the review queue

    @property
    def invoices(self) -> int:
        return self.passed + self.approved

    def as_dicts(self) -> list[dict]:
        return [dict(zip(IMPORT_COLUMNS, row)) for row in self.rows]


def import_rows(verdicts: list[Verdict]) -> ImportRows:
    """The rows of the import file: PASS invoices, plus any a person approved."""
    rows: list[list] = []
    passed = approved = 0
    for verdict in verdicts:
        if not verdict.importable:
            continue
        inv = verdict.invoice
        if verdict.status == APPROVED:
            approved += 1
        else:
            passed += 1
        for n, item in enumerate(inv.line_items, start=1):
            # Quantity and unit price as printed (0.125 stays 0.125); amounts to the cent.
            rows.append([
                safe_text(inv.vendor), safe_text(inv.invoice_number), _iso(inv.invoice_date),
                # Currency goes through safe_text too: the AI extractor takes it from the
                # document, so it is invoice text like the vendor name, not a constant.
                _iso(inv.due_date), safe_text(inv.currency), n, safe_text(item.description),
                fmt_number(item.quantity), price_str(item.unit_price), money_str(item.amount),
                money_str(inv.subtotal), money_str(inv.tax), money_str(inv.total),
                safe_text(inv.source_file),
            ])
    return ImportRows(rows, passed, approved)


def write_import_csv(verdicts: list[Verdict], path: Path) -> ImportRows:
    """One row per line item, for the invoices that may be imported."""
    built = import_rows(verdicts)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(IMPORT_COLUMNS)
        writer.writerows(built.rows)
    return built


def write_mapped_import_csv(built: ImportRows, mapping: ImportMapping, path: Path) -> None:
    """The same rows under the column names, and in the order, an accounting package wants.

    Constants from the mapping go through safe_text as well: an account code
    typed as "=400" would otherwise be a formula in the importer's spreadsheet.
    """
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([safe_text(t) for t in mapping.targets])
        for row in built.as_dicts():
            writer.writerow([safe_text(str(cell)) if isinstance(cell, str) else cell
                             for cell in map_row(mapping, row)])


def write_summary_csv(verdicts: list[Verdict], path: Path, *, unread: Sequence[Unread] = ()) -> None:
    """One row per file: every invoice with its status and reasons, then every file not checked."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SUMMARY_COLUMNS)
        for verdict in verdicts:
            inv = verdict.invoice
            reasons = [f"[{f.check}] {f.message}" for f in verdict.findings]
            if verdict.approval_note:
                reasons.append(f"[review_queue] {verdict.approval_note}")
            writer.writerow([
                safe_text(inv.source_file), verdict.status, safe_text(inv.vendor),
                safe_text(inv.invoice_number), _iso(inv.invoice_date), _iso(inv.due_date),
                money_str(inv.subtotal), money_str(inv.tax), money_str(inv.total),
                money_str(verdict.overcharge), money_str(verdict.repeat_total), len(inv.line_items),
                inv.layout, inv.extractor,
                safe_text(_approved_by(verdict)), _approved_at(verdict),
                safe_text(" | ".join(reasons)),
            ])
        for item in unread:
            writer.writerow([safe_text(item.name), NOT_CHECKED, *[""] * (len(SUMMARY_COLUMNS) - 3),
                             safe_text(item.reason)])


def _approved_by(verdict: Verdict) -> str:
    return verdict.approval.approved_by if verdict.approval is not None else ""


def _approved_at(verdict: Verdict) -> str:
    return verdict.approval.approved_at if verdict.approval is not None else ""


def _approved_reason(verdict: Verdict) -> str:
    """The free text the approver gave with --note, not to be confused with approval_note,
    which says why an approval on file could *not* be used."""
    return verdict.approval.note if verdict.approval is not None else ""


def write_exceptions_csv(verdicts: list[Verdict], path: Path, *, unread: Sequence[Unread] = ()) -> int:
    """The exceptions list: one row per flag, for everything that is not a clean PASS.

    Returns the number of rows written. A PASS invoice has no flags and is not
    on it. An approved invoice is: it reached the import file with flags on it,
    and its rows carry who approved it, so the sheet is the audit trail. Files
    that could not be read are on it too, as one `not_checked` row each.
    """
    written = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXCEPTION_COLUMNS)
        for verdict in verdicts:
            inv = verdict.invoice
            for finding in verdict.findings:
                difference, kind = _finding_money(finding)
                writer.writerow([
                    safe_text(inv.source_file), verdict.status, safe_text(inv.vendor),
                    safe_text(inv.invoice_number), _iso(inv.invoice_date), money_str(inv.total),
                    finding.check, safe_text(_label(finding.check)), safe_text(finding.message),
                    difference, kind,
                    money_str(verdict.overcharge), money_str(verdict.repeat_total),
                    safe_text(_approved_by(verdict)), _approved_at(verdict),
                    safe_text(_approved_reason(verdict)),
                    safe_text(verdict.approval_note or ""),
                ])
                written += 1
        for item in unread:
            writer.writerow([
                safe_text(item.name), NOT_CHECKED, "", "", "", "",
                NOT_CHECKED_CHECK, NOT_CHECKED_LABEL, safe_text(item.reason), "", "",
                "", "", "", "", "", "",
            ])
            written += 1
    return written


def _finding_money(finding) -> tuple[str, str]:
    """This one flag's money and what kind it is.

    These are the individual differences, not the invoice's counted figure:
    they are not netted, and they are shown even on an invoice whose figures
    the tool does not trust enough to count. The invoice's own counted figures
    are in InvoiceOverbilled and InvoiceSentTwice on every row.
    """
    if finding.repeat_total is not None:
        return money_str(finding.repeat_total), "sent twice"
    if not finding.overcharge:   # None, or a difference of exactly zero
        return "", ""
    kind = "overbilling" if finding.overcharge > 0 else "undercharge"
    return money_str(abs(finding.overcharge)), kind


def _md(text: str | None) -> str:
    return (text or "").replace("|", "\\|")


def _md_cell(text: str | None) -> str:
    """Free text for a Markdown table cell: pipes escaped and line breaks folded away.

    An approver's --note is typed by hand and can hold a line break, which would
    end the table row halfway through.
    """
    return _md(" ".join((text or "").split()))


def write_review_report(verdicts: list[Verdict], path: Path, *, today: date, input_label: str,
                        extractor: str, unread: Sequence[Unread] = ()) -> None:
    review = [v for v in verdicts if v.status == NEEDS_REVIEW]
    approved = [v for v in verdicts if v.status == APPROVED]
    passed = [v for v in verdicts if v.status == PASS]
    money = money_summary(verdicts)
    lines = [
        "# Invoice review report",
        "",
        f"- Checked: {len(verdicts)} invoices from `{input_label}` (extractor: `{extractor}`, "
        f"dates checked against {today.isoformat()})",
        f"- Result: {result_line(verdicts, unread)}",
        f"- {held_line(money)}",
        f"- {overbilling_line(money)}",
        *([f"- {sent_twice_line(money)}"] if money.repeat_copies else []),
        *([f"- {approved_line(money)}"] if money.approved_invoices else []),
        f"- {import_contents_line(money, tick='`')}",
        "",
        f"## Needs a human ({len(review)})",
        "",
    ]
    if not review:
        lines += ["Nothing needs review.", ""]
    for verdict in review:
        inv = verdict.invoice
        lines.append(f"### {inv.source_file}")
        lines.append("")
        facts = [
            f"Vendor: {inv.vendor or '(not found)'}",
            f"Invoice: {inv.invoice_number or '(not found)'}",
            f"Date: {_iso(inv.invoice_date) or inv.invoice_date_raw or '(not found)'}",
            f"Total: {fmt_money(inv.total) if inv.total is not None else '(not found)'}",
        ]
        note = _overbilled_note(verdict)
        if note:
            facts.append(f"Overbilled by: {note}")
        copy = _second_copy_note(verdict)
        if copy:
            facts.append(f"{copy}: paying this copy too would pay it twice")
        lines.append(" - ".join(facts))
        lines.append("")
        for finding in verdict.findings:
            lines.append(f"- **{_label(finding.check)}.** {finding.message} `{finding.check}`")
        if verdict.approval_note:
            lines.append(f"- **Review queue.** {verdict.approval_note} `review_queue`")
        lines.append("")
    if approved:
        lines += [f"## Approved by a person ({len(approved)})", "",
                  "These were held, then approved out of the review queue, and are in the import file.",
                  "", "| File | Invoice | Total | Held for | Approved by | When | Why |",
                  "|---|---|---:|---|---|---|---|"]
        for verdict in approved:
            inv = verdict.invoice
            a = verdict.approval
            held_for = ", ".join(dict.fromkeys(_label(f.check) for f in verdict.findings))
            lines.append(
                f"| {_md(inv.source_file)} | {_md(inv.invoice_number)} | {fmt_money(inv.total)} | "
                f"{_md(held_for)} | {_md_cell(a.approved_by)} | {_md_cell(a.approved_at)} | "
                f"{_md_cell(a.note) or '-'} |")
        lines.append("")
    lines += [f"## {NOT_CHECKED.capitalize()} ({len(unread)})", ""]
    if unread:
        lines += ["These files are in the folder but were not read, so nothing in them was checked.", "",
                  "| File | Why |", "|---|---|"]
        lines += [f"| {_md(item.name)} | {_md(item.reason)} |" for item in unread]
    else:
        lines.append("Every file in the folder was read.")
    lines += ["", f"## Passed ({len(passed)})", ""]
    if passed:
        lines += ["| File | Vendor | Invoice | Date | Lines | Total |",
                  "|---|---|---|---|---:|---:|"]
        for verdict in passed:
            inv = verdict.invoice
            lines.append(
                f"| {_md(inv.source_file)} | {_md(inv.vendor)} | {_md(inv.invoice_number)} | "
                f"{_iso(inv.invoice_date)} | {len(inv.line_items)} | {fmt_money(inv.total)} |"
            )
    else:
        lines.append("No invoices passed.")
    lines += ["", "## What every invoice was checked for", ""]
    lines += [f"- `{code}` ({LABELS[code]}): {description}" for code, description in CHECKS.items()]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


_HTML_STYLE = """
:root { color-scheme: light; --ink: #1d2433; --muted: #5b6475; --line: #e2e5eb; --page: #f5f6f8;
  --card: #ffffff; --pass: #1c7348; --pass-bg: #e5f3eb; --review: #ad2a1f; --review-bg: #fcebe8;
  --unread: #825400; --unread-bg: #fdf1d8; --twice: #1f4fa3; --twice-bg: #e6edf9;
  --approved: #5b3fa3; --approved-bg: #eeeaf8; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1000px; margin: 0 auto; padding: 28px 20px 40px; }
h1 { font-size: 26px; margin: 0 0 2px; }
h2 { font-size: 18px; margin: 30px 0 12px; }
.sub { color: var(--muted); margin: 0 0 20px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; }
.tile .n { font-size: 27px; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; }
.tile .l { color: var(--muted); font-size: 13px; }
.tile.pass .n { color: var(--pass); }
.tile.review .n, .tile.money .n { color: var(--review); }
.tile.unread .n { color: var(--unread); }
.tile.twice .n { color: var(--twice); }
.tile.approved .n { color: var(--approved); }
.label.queue { background: var(--approved-bg); color: var(--approved); }
.approved-table td:nth-child(5) { color: var(--approved); font-weight: 600; }
.tiles.money-row { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); margin-top: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--review);
  border-radius: 8px; padding: 12px 16px; margin-bottom: 10px; }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.file { font-weight: 600; }
.facts { color: var(--muted); font-size: 13.5px; }
.over { background: var(--review-bg); color: var(--review); font-weight: 600; font-size: 13px;
  border-radius: 999px; padding: 2px 10px; white-space: nowrap; }
.over.twice { background: var(--twice-bg); color: var(--twice); }
ul.reasons { list-style: none; margin: 8px 0 0; padding: 0; }
ul.reasons li { margin: 5px 0; }
.label { display: inline-block; background: var(--review-bg); color: var(--review); font-weight: 600;
  font-size: 12.5px; border-radius: 4px; padding: 1px 8px; margin-right: 6px; }
code { font: 12px Consolas, "SF Mono", Menlo, monospace; color: var(--muted); }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); font-size: 14px; }
th { color: var(--muted); font-weight: 600; font-size: 12.5px; background: #fafbfc; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.ok { color: var(--pass); font-weight: 600; }
.unread-table td:first-child { color: var(--unread); font-weight: 600; }
.none { color: var(--muted); }
details { margin-top: 30px; color: var(--muted); font-size: 14px; }
summary { cursor: pointer; font-weight: 600; color: var(--ink); }
footer { color: var(--muted); font-size: 13px; margin-top: 24px; }
"""


def _tile(kind: str, number: str, label: str) -> str:
    return (f'<div class="tile {kind}"><div class="n">{escape(number)}</div>'
            f'<div class="l">{escape(label)}</div></div>')


def write_review_html(verdicts: list[Verdict], path: Path, *, today: date, input_label: str,
                      extractor: str, unread: Sequence[Unread] = ()) -> None:
    """The review report as one self-contained page to open in a browser (no scripts, no links out)."""
    review = [v for v in verdicts if v.status == NEEDS_REVIEW]
    approved = [v for v in verdicts if v.status == APPROVED]
    passed = [v for v in verdicts if v.status == PASS]
    money = money_summary(verdicts)
    out = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Invoice review report</title>",
        f"<style>{_HTML_STYLE}</style></head><body><main>",
        "<h1>Invoice review report</h1>",
        f'<p class="sub">{len(verdicts)} invoices from <b>{escape(input_label)}</b> &middot; dates checked '
        f"against {today.isoformat()} &middot; extractor: {escape(extractor)}</p>",
        '<div class="tiles">',
        _tile("", str(len(verdicts)), "invoices checked"),
        _tile("pass", str(len(passed)), "passed, ready to import"),
        *([_tile("approved", str(len(approved)),
                 f"approved by a person ({fmt_money(money.approved_total)})")] if approved else []),
        _tile("review", str(len(review)), f"held for review ({fmt_money(money.held_total)})"),
        _tile("unread" if unread else "", str(len(unread)), "files not checked"),
        "</div>",
        # Overbilling and invoices sent twice side by side, never added together.
        '<div class="tiles money-row">',
        _tile("money", fmt_money(money.overcharge), "overbilling found (arithmetic errors)"),
        _tile("twice" if money.repeat_copies else "", fmt_money(money.repeat_total),
              _sent_twice_tile_label(money)),
        "</div>",
        f"<h2>Needs a human ({len(review)})</h2>",
    ]
    if not review:
        out.append('<p class="none">Nothing needs review.</p>')
    for verdict in review:
        inv = verdict.invoice
        facts = " &middot; ".join(escape(fact) for fact in (
            inv.vendor or "vendor not found",
            f"invoice {inv.invoice_number or '(not found)'}",
            _iso(inv.invoice_date) or inv.invoice_date_raw or "date not found",
            f"total {fmt_money(inv.total)}" if inv.total is not None else "total not found",
        ))
        note = _overbilled_note(verdict)
        badge = f'<span class="over">Overbilled by {escape(note)}</span>' if note else ""
        copy = _second_copy_note(verdict)
        if copy:
            badge += f'<span class="over twice">{escape(copy)}</span>'
        reasons = "".join(
            f'<li><span class="label">{escape(_label(f.check))}</span>{escape(f.message)} '
            f"<code>{escape(f.check)}</code></li>"
            for f in verdict.findings
        )
        if verdict.approval_note:
            reasons += (f'<li><span class="label queue">Review queue</span>'
                        f"{escape(verdict.approval_note)} <code>review_queue</code></li>")
        out.append(
            f'<div class="card"><div class="head"><div><div class="file">{escape(inv.source_file)}</div>'
            f'<div class="facts">{facts}</div></div>{badge}</div><ul class="reasons">{reasons}</ul></div>'
        )
    if approved:
        out.append(f"<h2>Approved by a person ({len(approved)})</h2>")
        out.append('<p class="sub">Held by a check, then approved out of the review queue. '
                   "These are in the import file.</p>")
        out.append('<table class="approved-table"><tr><th>File</th><th>Invoice</th>'
                   '<th class="num">Total</th><th>Held for</th><th>Approved by</th><th>When</th>'
                   "<th>Why</th></tr>")
        for verdict in approved:
            inv = verdict.invoice
            a = verdict.approval
            held_for = ", ".join(dict.fromkeys(_label(f.check) for f in verdict.findings))
            out.append(
                f"<tr><td>{escape(inv.source_file)}</td><td>{escape(inv.invoice_number or '')}</td>"
                f'<td class="num">{fmt_money(inv.total)}</td><td>{escape(held_for)}</td>'
                f"<td>{escape(a.approved_by)}</td><td>{escape(a.approved_at)}</td>"
                f"<td>{escape(a.note) or '&ndash;'}</td></tr>")
        out.append("</table>")
    out.append(f"<h2>Not checked ({len(unread)})</h2>")
    if unread:
        out.append('<table class="unread-table"><tr><th>File</th><th>Why it was not checked</th></tr>')
        out += [f"<tr><td>{escape(item.name)}</td><td>{escape(item.reason)}</td></tr>" for item in unread]
        out.append("</table>")
    else:
        out.append('<p class="none">Every file in the folder was read.</p>')
    out.append(f"<h2>Passed ({len(passed)})</h2>")
    if passed:
        out.append('<table><tr><th>File</th><th>Vendor</th><th>Invoice</th><th>Date</th>'
                   '<th class="num">Lines</th><th class="num">Total</th></tr>')
        for verdict in passed:
            inv = verdict.invoice
            out.append(
                f'<tr><td class="ok">{escape(inv.source_file)}</td><td>{escape(inv.vendor or "")}</td>'
                f"<td>{escape(inv.invoice_number or '')}</td><td>{_iso(inv.invoice_date)}</td>"
                f'<td class="num">{len(inv.line_items)}</td><td class="num">{fmt_money(inv.total)}</td></tr>'
            )
        out.append("</table>")
    else:
        out.append('<p class="none">No invoices passed.</p>')
    out.append(f"<details><summary>What every invoice was checked for ({len(CHECKS)} checks)</summary><ul>")
    out += [f"<li><b>{escape(_label(code))}</b> <code>{code}</code>: {escape(description)}</li>"
            for code, description in CHECKS.items()]
    out += ["</ul></details>",
            f"<footer>{escape(import_contents_line(money))} Every other file is listed above "
            "with its reason.</footer>",
            "</main></body></html>", ""]
    path.write_text("\n".join(out), encoding="utf-8")


def _money_or_none(value) -> str | None:
    return money_str(value) if value is not None else None


def write_results_json(verdicts: list[Verdict], path: Path, *, today: date, input_label: str,
                       extractor: str, unread: Sequence[Unread] = ()) -> None:
    money = money_summary(verdicts)
    data = {
        "checked_against_date": today.isoformat(),
        "input": input_label,
        "extractor": extractor,
        "counts": {
            PASS: sum(v.status == PASS for v in verdicts),
            APPROVED: sum(v.status == APPROVED for v in verdicts),
            NEEDS_REVIEW: sum(v.status == NEEDS_REVIEW for v in verdicts),
            NOT_CHECKED: len(unread),
        },
        "money": {
            "held_for_review_total": money_str(money.held_total),
            # Arithmetic errors in the vendor's favour. Invoices sent twice are not in it.
            "overbilling_found": money_str(money.overcharge),
            "sent_twice_copies": money.repeat_copies,
            "sent_twice_total": money_str(money.repeat_total),
            # Invoices a person approved out of the review queue, and the overbilling
            # among them: money that reached the import file with a name against it.
            "approved_invoices": money.approved_invoices,
            "approved_total": money_str(money.approved_total),
            "approved_overbilling": money_str(money.approved_overcharge),
        },
        "invoices": [
            {
                "status": v.status,
                "overbilled": money_str(v.overcharge),
                "sent_twice_amount": money_str(v.repeat_total),
                # What an approval is tied to. `invoicecheck approve` reads it from here.
                "content_hash": invoice_hash(v.invoice),
                "approval": v.approval.to_dict() if v.approval is not None else None,
                "approval_note": v.approval_note,
                "findings": [{"check": f.check, "label": LABELS[f.check], "message": f.message,
                              "overcharge": _money_or_none(f.overcharge),
                              "sent_twice_amount": _money_or_none(f.repeat_total)} for f in v.findings],
                "invoice": v.invoice.to_dict(),
            }
            for v in verdicts
        ],
        "not_checked": [{"file": item.name, "reason": item.reason} for item in unread],
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
