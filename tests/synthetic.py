"""Synthetic invoice batches with a known answer for every file.

make_batch() writes invoices in the four sample layouts, with random vendors,
items, quantities, prices, tax rates and dates, and plants one mistake of a
known kind in about a third of them. It returns the answer key: for each file,
the reason codes a correct checker gives, the overbilling it should find, and,
for the reminder copy of an invoice sent twice, the total that must not be paid
twice (reported apart from overbilling).
score() compares a run's results.json with that key.

Used by tests/test_synthetic.py (a small batch on every test run) and by
tools/benchmark.py (a large, timed one). The generator was written alongside
the checker, so a perfect score shows the checks hold up across many random
numbers and all four layouts, not that they read layouts they have never seen.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

TODAY = date(2026, 9, 21)
CENT = Decimal("0.01")

# The planted mistakes, and the reasons a correct checker gives for each.
EXPECTED_CHECKS = {
    None: frozenset(),
    "line_math": frozenset({"line_math"}),              # one line's amount is not quantity x price
    "lines_sum": frozenset({"lines_sum"}),              # the subtotal is more than the lines
    "tax_rate": frozenset({"tax_rate"}),                # tax worked out at a different rate
    "total_math": frozenset({"total_math"}),            # the total is more than subtotal + tax
    "date_typo": frozenset({"date_future", "due_before_issue"}),   # year typed 36 years ahead
    "stale_date": frozenset({"date_stale"}),            # more than a year old
    "sent_twice": frozenset({"duplicate_number"}),      # resent as a payment reminder
}
MISTAKES = tuple(k for k in EXPECTED_CHECKS if k is not None)
LAYOUTS = ("text-columns", "text-pipe-table", "html-table", "html-list")

_NAMES = ("Amberly Birchcombe Cobalt Dunmere Elmstead Foxglove Garnet Hollowell Ivybridge Juniper "
          "Kettlewell Larkspur Marlow Nettlefold Oakhurst Pebbleford Quarrytop Rushmere Saltmarsh "
          "Thistledown Umberlane Vellacott Willowby Yarrowdale").split()
_TRADES = ("Office Supply", "Catering", "Plumbing", "IT Services", "Print Works", "Cleaning",
           "Electrical", "Landscaping", "Couriers", "Coffee Roasters")
_SUFFIXES = ("", " LLC", " Inc.", " Co.", " Ltd")
_ITEMS = ("Copy paper case", "Toner cartridge", "Service call", "Labour (hours)", "Cleaning visit",
          "Window cleaning", "Buffet lunch, per guest", "Coffee service", "Cable kit",
          "Backup subscription", "Lawn mowing visit", "Hedge trimming", "Delivery run", "Parts kit",
          "Monthly support, per seat", "Filter replacement", "Poster print, A2", "Business cards, box")
_CUSTOMERS = ("Thimbleburrow Veterinary Clinic", "Inkwhistle Design Studio", "Glimmerwick Theatre",
              "Tidepool Dental Studio", "Mapleknot Bakery", "Driftmoor Yoga Loft")
_RATES = tuple(Decimal(r) for r in ("5", "6", "7.25", "8", "8.875", "9.5"))
_BANNER = "PAYMENT REMINDER - SECOND NOTICE"


@dataclass(frozen=True)
class Expected:
    mistake: str | None
    checks: frozenset
    overbilled: Decimal
    sent_twice: Decimal = Decimal("0")   # the whole total, on the reminder copy only


@dataclass
class _Invoice:
    vendor: str
    layout: str
    number: str
    customer: str
    issued: date
    due: date
    printed_date: date
    rate: Decimal
    lines: list = field(default_factory=list)   # [description, quantity, unit price, amount]
    subtotal: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    total: Decimal = Decimal("0")


def _cents(value: Decimal) -> Decimal:
    # The generator's own rounding (half up, as a till does), so the answer key
    # does not depend on the code it is checking.
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _m(value: Decimal) -> str:
    return f"{value:,.2f}"


def _q(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _vendors(rng: random.Random, count: int) -> list[tuple[str, str, str]]:
    pairs = rng.sample([(n, t) for n in _NAMES for t in _TRADES], count)
    return [(f"{n} {t}{rng.choice(_SUFFIXES)}", LAYOUTS[i % len(LAYOUTS)], f"{n[0]}{t[0]}")
            for i, (n, t) in enumerate(pairs)]


def _build(rng: random.Random, vendor: tuple[str, str, str], serial: int, mistake: str | None) -> tuple[_Invoice, Decimal]:
    name, layout, prefix = vendor
    issued = TODAY - timedelta(days=rng.randint(366, 700) if mistake == "stale_date" else rng.randint(1, 300))
    inv = _Invoice(vendor=name, layout=layout, number=f"{prefix}-{serial}", customer=rng.choice(_CUSTOMERS),
                   issued=issued, due=issued + timedelta(days=rng.choice((14, 30))), printed_date=issued,
                   rate=rng.choice(_RATES))
    for desc in rng.sample(_ITEMS, rng.randint(1, 5)):
        qty = Decimal(rng.randint(1, 16)) / 2 if "hours" in desc else Decimal(rng.randint(1, 24))
        unit = (Decimal(rng.randint(500, 25000)) / 100).quantize(CENT)
        inv.lines.append([desc, qty, unit, _cents(qty * unit)])

    # A planted difference: small (2 to 99 cents, like transposed cents) or large ($1 to $50).
    extra = (Decimal(rng.randint(2, 99) if rng.random() < 0.5 else rng.randint(100, 5000)) / 100).quantize(CENT)
    overbilled = Decimal("0")
    if mistake == "line_math":
        rng.choice(inv.lines)[3] += extra
        overbilled = extra
    inv.subtotal = sum((line[3] for line in inv.lines), Decimal("0"))
    if mistake == "lines_sum":
        inv.subtotal += extra
        overbilled = extra
    inv.tax = _cents(inv.subtotal * inv.rate / 100)
    if mistake == "tax_rate":
        # An old or neighbouring rate, clear of the documented rounding allowance
        # (a cent per two lines); a change too small to clear it falls back to +2 points.
        allowance = CENT * max(1, math.ceil(len(inv.lines) / 2))
        wrong = inv.rate + Decimal(rng.choice(("-2", "-1", "-0.5", "-0.25", "0.25", "0.5", "1", "2")))
        charged = _cents(inv.subtotal * wrong / 100)
        if abs(charged - inv.tax) <= allowance:
            charged = _cents(inv.subtotal * (inv.rate + 2) / 100)
        overbilled = max(charged - inv.tax, Decimal("0"))
        inv.tax = charged
    inv.total = inv.subtotal + inv.tax
    if mistake == "total_math":
        inv.total += extra
        overbilled = extra
    if mistake == "date_typo":
        inv.printed_date = issued.replace(year=issued.year + 36)
        inv.due = issued + timedelta(days=30)
    return inv, overbilled


def _render_text_columns(inv: _Invoice, banner: bool) -> str:
    rows = [f"{desc:<34}{_q(qty):>6}{_m(unit):>13}{_m(amount):>12}" for desc, qty, unit, amount in inv.lines]
    rule = "-" * 65
    return "\n".join(([_BANNER] if banner else []) + [
        inv.vendor, "418 Fernhollow Road, Quillmere", "Tel 555-0142", "", "INVOICE", "",
        f"Invoice Number:   {inv.number}",
        f"Invoice Date:     {inv.printed_date.isoformat()}",
        f"Due Date:         {inv.due.isoformat()}",
        f"Bill To:          {inv.customer}", "",
        "Description                          Qty   Unit Price      Amount", rule, *rows, rule,
        f"{'Subtotal:':>52}{_m(inv.subtotal):>13}",
        f"{f'Tax ({_q(inv.rate)}%):':>52}{_m(inv.tax):>13}",
        f"{'Total Due:':>52}{_m(inv.total):>13}", "",
        "Payment terms: Net 30. Thank you for your business.", "",
    ])


def _render_pipe_table(inv: _Invoice, banner: bool) -> str:
    rows = [f"| SK-{n:03d} | {desc} | {_q(qty)} | ${_m(unit)} | ${_m(amount)} |"
            for n, (desc, qty, unit, amount) in enumerate(inv.lines, start=100)]
    rule = "=" * 49
    return "\n".join((["COPY"] if banner else []) + [
        inv.vendor, "Serving small offices since 2009", rule,
        f"INVOICE #: {inv.number}",
        f"Invoice Date: {inv.printed_date:%m/%d/%Y}",
        f"Payment Due: {inv.due:%m/%d/%Y}",
        f"Customer: {inv.customer}", rule, "",
        "| SKU | Item | Qty | Rate | Line Total |", "|---|---|---|---|---|", *rows, "",
        f"SUBTOTAL:              ${_m(inv.subtotal)}",
        f"SALES TAX ({_q(inv.rate)}%):   ${_m(inv.tax)}",
        f"AMOUNT DUE:            ${_m(inv.total)}", "",
        "Please quote the invoice number with your payment.", "",
    ])


def _long_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _render_html_table(inv: _Invoice, banner: bool) -> str:
    rows = "\n".join(f"<tr><td>{desc}</td><td>{_q(qty)}</td><td>${_m(unit)}</td><td>${_m(amount)}</td></tr>"
                     for desc, qty, unit, amount in inv.lines)
    notice = f'<p class="notice">{_BANNER}</p>\n' if banner else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Invoice {inv.number}</title></head>
<body>
{notice}<div class="header"><h1 class="vendor-name">{inv.vendor}</h1><p>77 Wharf Street, Port Elden &middot; 555-0178</p></div>
<table class="meta">
<tr><th>Invoice No.</th><td>{inv.number}</td></tr>
<tr><th>Date</th><td>{_long_date(inv.printed_date)}</td></tr>
<tr><th>Due</th><td>{_long_date(inv.due)}</td></tr>
<tr><th>Client</th><td>{inv.customer}</td></tr>
</table>
<table class="line-items">
<thead><tr><th>Item</th><th>Quantity</th><th>Price</th><th>Total</th></tr></thead>
<tbody>
{rows}
</tbody>
<tfoot>
<tr><td colspan="3">Subtotal</td><td>${_m(inv.subtotal)}</td></tr>
<tr><td colspan="3">Sales tax ({_q(inv.rate)}%)</td><td>${_m(inv.tax)}</td></tr>
<tr><td colspan="3"><strong>Total</strong></td><td><strong>${_m(inv.total)}</strong></td></tr>
</tfoot>
</table>
<p class="terms">Payment within 30 days. Thank you!</p>
</body></html>
"""


def _render_html_list(inv: _Invoice, banner: bool) -> str:
    items = "\n".join(f"<li>{_q(qty)} x {desc} @ ${_m(unit)} = ${_m(amount)}</li>"
                      for desc, qty, unit, amount in inv.lines)
    notice = f"<h1>{_BANNER}</h1>\n" if banner else ""
    return f"""<html><body>
<header>
{notice}<h2>{inv.vendor}</h2>
<p>Commercial services &middot; 555-0193</p>
</header>
<dl>
<dt>Invoice No.</dt><dd>{inv.number}</dd>
<dt>Issued</dt><dd>{inv.printed_date.day} {inv.printed_date:%b %Y}</dd>
<dt>Payment due</dt><dd>{inv.due.day} {inv.due:%b %Y}</dd>
</dl>
<ul>
{items}
</ul>
<section class="totals">
<p>Subtotal: ${_m(inv.subtotal)}</p>
<p>Tax {_q(inv.rate)}%: ${_m(inv.tax)}</p>
<p>Total: ${_m(inv.total)}</p>
</section>
</body></html>
"""


_RENDER = {
    "text-columns": (_render_text_columns, "txt"),
    "text-pipe-table": (_render_pipe_table, "txt"),
    "html-table": (_render_html_table, "html"),
    "html-list": (_render_html_list, "html"),
}


def make_batch(folder: Path, count: int, seed: int = 1) -> dict[str, Expected]:
    """Write `count` invoices (plus a reminder copy for each one sent twice) and return the answer key."""
    rng = random.Random(seed)
    vendors = _vendors(rng, min(len(_NAMES) * len(_TRADES), max(4, count // 25)))
    folder.mkdir(parents=True, exist_ok=True)
    key: dict[str, Expected] = {}
    for serial in range(10001, 10001 + count):
        mistake = rng.choice(MISTAKES) if rng.random() < 1 / 3 else None
        vendor = rng.choice(vendors)
        inv, overbilled = _build(rng, vendor, serial, mistake)
        render, ext = _RENDER[inv.layout]
        stem = f"{vendor[0].split()[0].lower()}_{inv.number}"
        (folder / f"{stem}.{ext}").write_text(render(inv, False), encoding="utf-8")
        checks = EXPECTED_CHECKS[mistake]
        if mistake == "sent_twice":
            (folder / f"{stem}_reminder.{ext}").write_text(render(inv, True), encoding="utf-8")
            key[f"{stem}.{ext}"] = Expected(mistake, checks, Decimal("0"))
            key[f"{stem}_reminder.{ext}"] = Expected(mistake, checks, Decimal("0"), inv.total)
        else:
            key[f"{stem}.{ext}"] = Expected(mistake, checks, overbilled)
    return key


@dataclass
class Score:
    invoices: int = 0          # distinct invoices generated
    files: int = 0             # files written (a reminder copy is a second file)
    should_pass: int = 0
    passed: int = 0
    false_alarms: int = 0      # clean files held for review
    should_hold: int = 0
    held_right: int = 0        # held with exactly the expected reasons
    missed: int = 0            # a planted mistake that passed
    wrong_reason: int = 0      # held, but not for the expected reasons
    overbilling_planted: Decimal = Decimal("0")
    overbilling_found: Decimal = Decimal("0")
    sent_twice_planted: int = 0          # reminder copies written
    sent_twice_planted_total: Decimal = Decimal("0")
    sent_twice_found: int = 0
    sent_twice_found_total: Decimal = Decimal("0")
    problems: list[str] = field(default_factory=list)

    @property
    def perfect(self) -> bool:
        return (not self.false_alarms and not self.missed and not self.wrong_reason
                and self.overbilling_found == self.overbilling_planted
                and self.sent_twice_found == self.sent_twice_planted
                and self.sent_twice_found_total == self.sent_twice_planted_total and not self.problems)


def score(key: dict[str, Expected], results: dict) -> Score:
    s = Score(files=len(key), invoices=sum(1 for name in key if not name.split(".")[0].endswith("_reminder")))
    seen = set()
    for item in results["invoices"]:
        name = item["invoice"]["source_file"]
        seen.add(name)
        expected = key.get(name)
        if expected is None:
            s.problems.append(f"{name}: not in the answer key")
            continue
        checks = frozenset(f["check"] for f in item["findings"])
        held = item["status"] == "NEEDS REVIEW"
        if expected.checks:
            s.should_hold += 1
            if not held:
                s.missed += 1
                s.problems.append(f"{name}: planted {expected.mistake}, but it passed")
            elif checks == expected.checks:
                s.held_right += 1
            else:
                s.wrong_reason += 1
                s.problems.append(f"{name}: planted {expected.mistake}, held for {sorted(checks)}")
        else:
            s.should_pass += 1
            if held:
                s.false_alarms += 1
                s.problems.append(f"{name}: clean, held for {sorted(checks)}")
            else:
                s.passed += 1
        if Decimal(item["overbilled"]) != expected.overbilled:
            s.problems.append(f"{name}: overbilled {item['overbilled']}, expected {expected.overbilled}")
        if Decimal(item["sent_twice_amount"]) != expected.sent_twice:
            s.problems.append(f"{name}: sent twice {item['sent_twice_amount']}, expected {expected.sent_twice}")
        s.overbilling_planted += expected.overbilled
        if expected.sent_twice:
            s.sent_twice_planted += 1
            s.sent_twice_planted_total += expected.sent_twice
    s.overbilling_found = Decimal(results["money"]["overbilling_found"])
    s.sent_twice_found = results["money"]["sent_twice_copies"]
    s.sent_twice_found_total = Decimal(results["money"]["sent_twice_total"])
    s.problems += [f"{name}: in the answer key but not in the results" for name in sorted(set(key) - seen)]
    return s
