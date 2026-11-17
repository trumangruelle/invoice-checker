"""Deterministic, offline extraction for the invoice layouts in samples/.

Each layout has a detector and a parser. Detection is by structure, not by
vendor name, so a vendor whose invoices use a known layout does not need a
parser of its own.

When a parser meets something it cannot account for (a row it cannot read,
an item-shaped row without cents, an amount in the totals block that is not
a subtotal, tax or total), it adds a warning to the invoice, and any warning
sends the invoice to NEEDS REVIEW.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .documents import Document
from .htmldoc import Node, row_cells, to_text
from .model import Invoice, LineItem
from .textutil import (
    MONEY_TOKEN,
    clean,
    last_money,
    parse_date,
    parse_money,
    parse_percent,
    parse_quantity,
)

# --------------------------------------------------------------------------
# Header labels
# --------------------------------------------------------------------------

# A bare "Date" or "Due" only counts as a label when a separator, the end of
# the line, or a date follows it. "Date of service:" and "Due to ..." do not.
_VALUE_FOLLOWS = (
    r"(?=\s*(?:[:#.\-]|$)"
    r"|\s+(?:\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d))"
)

# In priority order: a specific label ("Invoice Date") anywhere in the header
# wins over a bare "Date", whichever comes first on the page.
_LABELS = (
    ("invoice_number", r"(?:invoice|inv\.?)\s*(?:number|no\.?|num\.?|#)"),
    ("due_date", r"(?:due\s+date|payment\s+due|due\s+by|pay\s+by)"),
    ("invoice_date", r"(?:invoice\s+date|date\s+of\s+issue|issue\s+date|date\s+issued|issued(?:\s+on)?)"),
    ("due_date", rf"due{_VALUE_FOLLOWS}"),
    ("invoice_date", rf"date{_VALUE_FOLLOWS}"),
)
_LABEL_RES = tuple(
    (field, re.compile(rf"^\s*{pattern}(?![A-Za-z])\s*[:#.\-]?\s*(?P<value>.*)$", re.IGNORECASE))
    for field, pattern in _LABELS
)


def _label_value(regex: re.Pattern, line: str) -> str | None:
    """The value after a label such as "Invoice #:" or "Due Date", or None."""
    m = regex.match(line)
    if not m:
        return None
    # Values end at a run of 2+ spaces (a second column on the same line).
    return re.split(r"\s{2,}", m["value"].strip())[0].strip()


def header_fields(lines: list[str]) -> dict[str, str]:
    """Invoice number and dates from "Label: value" lines.

    A label alone on its line takes its value from the next line. Each line
    supplies at most one field.
    """
    found: dict[str, str] = {}
    used: set[int] = set()
    for field, regex in _LABEL_RES:
        if field in found:
            continue
        for i, line in enumerate(lines):
            if i in used:
                continue
            value = _label_value(regex, line)
            if value is None:
                continue
            used.add(i)
            if not value and i + 1 < len(lines):
                value = lines[i + 1].strip()
                used.add(i + 1)
            found[field] = value
            break
    return found


def apply_header(inv: Invoice, fields: dict[str, str]) -> None:
    number = fields.get("invoice_number")
    if number:
        inv.invoice_number = number.lstrip("#").strip() or None
    raw = fields.get("invoice_date")
    if raw:
        inv.invoice_date_raw = raw
        inv.invoice_date = parse_date(raw)
    raw = fields.get("due_date")
    if raw:
        inv.due_date_raw = raw
        inv.due_date = parse_date(raw)


# --------------------------------------------------------------------------
# Vendor
# --------------------------------------------------------------------------

# Words that label the document rather than name anyone. A line made only of
# these ("INVOICE", "COPY", "PAYMENT REMINDER - SECOND NOTICE", "Page 1 of 2")
# is never taken as the vendor, so a banner added to a resent invoice does not
# change the vendor name the duplicate checks match on.
_BANNER_WORDS = frozenset(
    "invoice invoices tax copy duplicate reprint reprinted original resent resend reminder "
    "payment notice second third final friendly overdue past due urgent statement page of "
    "customer account remittance advice proforma pro forma".split()
)
# "Payment reminder: this invoice is now overdue..." - a banner phrase, then a separator.
_BANNER_LEADS = frozenset(
    "copy duplicate reprint reprinted resent resend reminder notice overdue statement".split()
)
_BANNER_SPLIT = re.compile(r"\s*(?::|!|\s[-–—]\s)")
_WORDS = re.compile(r"[a-z]+|\d+")


def is_banner(line: str) -> bool:
    words = _WORDS.findall(clean(line).lower())
    if not words:
        return True  # rules and decoration: "*****", "#"
    if all(w.isdigit() or w in _BANNER_WORDS for w in words):
        return True
    lead = _BANNER_SPLIT.split(clean(line).lower(), maxsplit=1)
    if len(lead) == 2:
        lead_words = _WORDS.findall(lead[0])
        return (bool(lead_words) and all(w.isdigit() or w in _BANNER_WORDS for w in lead_words)
                and any(w in _BANNER_LEADS for w in lead_words))
    return False


_VENDOR_LABEL = re.compile(
    r"^\s*(?:vendor|supplier|seller|sold\s+by|billed\s+by|payee)(?:\s+name)?\s*:\s*(?P<value>\S.*)$",
    re.IGNORECASE,
)


def labelled_vendor(lines: list[str]) -> str | None:
    """A vendor printed with a label ("Vendor: ...", "Supplier: ..."), which beats position."""
    for line in lines:
        m = _VENDOR_LABEL.match(line)
        if m:
            value = clean(re.split(r"\s{2,}", m["value"].strip())[0])
            if value and not is_banner(value):
                return value
    return None


def vendor_from_lines(lines: list[str]) -> str | None:
    """Vendor from the text above the items: a labelled vendor, else the first line that is not a banner."""
    labelled = labelled_vendor(lines)
    if labelled:
        return labelled
    for line in lines:
        if line.strip() and not _SEPARATOR.match(line) and not is_banner(line):
            return clean(line)
    return None


_VENDOR_CLASS = re.compile(r"(?:vendor|company|supplier|seller)(?:[-_]?name)?", re.IGNORECASE)


def vendor_from_dom(root: Node, lines: list[str] | None = None) -> str | None:
    """Vendor from HTML: a vendor/company class, then a labelled vendor, then the first h1/h2 that is not a banner."""
    for node in root.iter():
        if node.has_ancestor("head") or not any(_VENDOR_CLASS.fullmatch(c) for c in node.classes):
            continue
        for line in to_text(node).splitlines():
            if not is_banner(line):
                return clean(line)
    labelled = labelled_vendor(label_lines(root) if lines is None else lines)
    if labelled:
        return labelled
    for node in root.iter():
        if node.tag in ("h1", "h2") and not node.has_ancestor("head"):
            text = node.text()
            if text and not is_banner(text):
                return text
    return None


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------

_SUBTOTAL = re.compile(r"\bsub[\s-]?total\b", re.IGNORECASE)
_TAX = re.compile(r"\b(?:sales\s+)?(?:tax|vat|gst|hst)\b", re.IGNORECASE)
_TOTAL = re.compile(r"\b(?:grand\s+)?total\b|\bamount\s+due\b|\bbalance\s+due\b", re.IGNORECASE)


def classify_total_line(text: str) -> tuple[str, Decimal, Decimal | None] | None:
    """("subtotal" | "tax" | "total", amount, tax rate %) for a totals line."""
    amount = last_money(text)
    if amount is None:
        return None
    if _SUBTOTAL.search(text):
        return "subtotal", amount, None
    if _TAX.search(text):
        return "tax", amount, parse_percent(text)
    if _TOTAL.search(text):
        return "total", amount, None
    return None


def apply_total(inv: Invoice, kind: str, amount: Decimal, rate: Decimal | None) -> None:
    """Record a totals value. The first value of each kind wins; a repeat is a warning."""
    current = getattr(inv, kind)
    if current is not None:
        if current != amount:
            inv.warnings.append(
                f"found two different {kind} values ({current} and {amount}); kept the first"
            )
        return
    setattr(inv, kind, amount)
    if kind == "tax" and rate is not None:
        inv.tax_rate = rate


def read_totals_line(inv: Invoice, text: str, where: str) -> None:
    """A line from a totals block: record a subtotal, tax or total, or warn about any other amount.

    Shipping, freight, fees, discounts and deposits are not part of the
    arithmetic checks, so an amount like that is named in a warning instead
    of being left out quietly.
    """
    total = classify_total_line(text)
    if total:
        apply_total(inv, *total)
    elif MONEY_TOKEN.search(text):
        warn_unlabelled_amount(inv, text, where)


def warn_unlabelled_amount(inv: Invoice, text: str, where: str) -> None:
    inv.warnings.append(
        f"{where}: {clean(text)!r} is an amount that is not a subtotal, tax or total, "
        f"so the arithmetic checks do not include it"
    )


# --------------------------------------------------------------------------
# Line-item columns (pipe tables and HTML tables)
# --------------------------------------------------------------------------

_COLUMN_PATTERNS = {
    "description": re.compile(r"(?:item|description|details|service|product|particulars)s?(?:\s+description)?"),
    "quantity": re.compile(r"qty\.?|quantity|units?|hours|hrs"),
    "unit_price": re.compile(r"(?:unit\s*)?(?:price|rate|cost)|each|unit\s+cost"),
    "amount": re.compile(r"(?:line\s*)?(?:total|amount)|ext(?:ended|\.)?\s*(?:price|amount)?"),
}


def map_columns(header: list[str]) -> dict[str, int] | None:
    """Column index for description / quantity / unit_price / amount, or None."""
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        name = clean(cell).lower().rstrip(":")
        for column, pattern in _COLUMN_PATTERNS.items():
            if column not in mapping and pattern.fullmatch(name):
                mapping[column] = index
                break
    if set(mapping) == set(_COLUMN_PATTERNS):
        return mapping
    return None


def item_from_cells(cells: list[str], columns: dict[str, int], where: str,
                    inv: Invoice, source_line: int) -> LineItem | None:
    try:
        desc = clean(cells[columns["description"]])
        qty = parse_quantity(cells[columns["quantity"]])
        unit = parse_money(cells[columns["unit_price"]])
        amount = parse_money(cells[columns["amount"]])
    except IndexError:
        inv.warnings.append(f"{where}: could not read an item row: {cells}")
        return None
    if not desc or qty is None or unit is None or amount is None:
        inv.warnings.append(f"{where}: could not read an item row: {cells}")
        return None
    return LineItem(desc, qty, unit, amount, source_line)


# --------------------------------------------------------------------------
# Layout A: plain text with whitespace-aligned columns
# --------------------------------------------------------------------------

_A_HEADER = re.compile(
    r"^\s*(?:description|item|details)\b.*\bqty\b.*\b(?:unit\s*price|price|rate)\b.*\b(?:amount|total)\b",
    re.IGNORECASE,
)
_A_ROW = re.compile(
    r"^\s*(?P<desc>\S.*?)\s{2,}(?P<qty>\d[\d,]*(?:\.\d+)?)\s+"
    r"(?P<unit>\$?\s?[\d,]+\.\d{2,4})\s+(?P<amount>\(?-?\$?\s?[\d,]+\.\d{2}\)?)\s*$"
)
_SEPARATOR = re.compile(r"^\s*[-=_*~]{3,}\s*$")
# A number standing in a column of its own at the end of a line ("Delivery   1   15   15").
# A wrapped description ("(white, 110 lb)") does not end like that.
_COLUMN_NUMBER = re.compile(r"\S\s{2,}\(?-?\$?\d[\d,]*(?:\.\d+)?\)?\s*$")


def detect_text_columns(doc: Document) -> bool:
    return doc.kind == "text" and any(_A_HEADER.match(line) for line in doc.text.splitlines())


def parse_text_columns(doc: Document) -> Invoice:
    lines = doc.text.splitlines()
    header_at = next(i for i, line in enumerate(lines) if _A_HEADER.match(line))
    inv = Invoice(source_file=doc.name, layout="text-columns")
    inv.vendor = vendor_from_lines(lines[:header_at])
    apply_header(inv, header_fields(lines[:header_at]))

    in_totals = False
    for number, line in enumerate(lines[header_at + 1:], start=header_at + 2):
        if not line.strip() or _SEPARATOR.match(line):
            continue
        where = f"line {number}"
        # Item rows are tried first, so "Tax filing folders ... 63.50" is an item, not the tax.
        m = _A_ROW.match(line)
        if m and not in_totals:
            inv.line_items.append(LineItem(
                clean(m["desc"]), parse_quantity(m["qty"]),
                parse_money(m["unit"]), parse_money(m["amount"]), number,
            ))
            continue
        total = classify_total_line(line)
        if total:
            in_totals = True
            apply_total(inv, *total)
        elif m:
            inv.warnings.append(f"{where}: an item row after the totals was not read: {clean(line)!r}")
        elif in_totals:
            if MONEY_TOKEN.search(line):
                warn_unlabelled_amount(inv, line, where)
            # anything else below the totals is footer text (payment terms, thanks)
        elif MONEY_TOKEN.search(line) or _COLUMN_NUMBER.search(line):
            inv.warnings.append(f"{where}: could not read an item row: {clean(line)!r}")
        elif inv.line_items:
            # No amount on the line: a wrapped description.
            inv.line_items[-1].description += " " + clean(line)
    return inv


# --------------------------------------------------------------------------
# Layout B: plain text with a pipe-delimited table
# --------------------------------------------------------------------------

_PIPE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*$")


def _pipe_cells(line: str) -> list[str]:
    return [clean(c) for c in line.strip().strip("|").split("|")]


def _pipe_header_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and not _PIPE_SEPARATOR.match(line):
            if map_columns(_pipe_cells(line)):
                return i
    return None


def detect_text_pipe_table(doc: Document) -> bool:
    return doc.kind == "text" and _pipe_header_index(doc.text.splitlines()) is not None


def parse_text_pipe_table(doc: Document) -> Invoice:
    lines = doc.text.splitlines()
    header_at = _pipe_header_index(lines)
    header = _pipe_cells(lines[header_at])
    columns = map_columns(header)
    inv = Invoice(source_file=doc.name, layout="text-pipe-table")
    inv.vendor = vendor_from_lines(lines[:header_at])
    apply_header(inv, header_fields(lines[:header_at]))

    for number, line in enumerate(lines[header_at + 1:], start=header_at + 2):
        if not line.strip() or _PIPE_SEPARATOR.match(line):
            continue
        if line.lstrip().startswith("|"):
            cells = _pipe_cells(line)
            qty_cell = cells[columns["quantity"]] if len(cells) > columns["quantity"] else ""
            if len(cells) != len(header) or not qty_cell:
                # A totals row inside the table, or a row that does not fit.
                total = classify_total_line(" ".join(cells))
                if total:
                    apply_total(inv, *total)
                else:
                    inv.warnings.append(f"line {number}: could not read an item row: {cells}")
                continue
            item = item_from_cells(cells, columns, f"line {number}", inv, number)
            if item:
                inv.line_items.append(item)
            continue
        read_totals_line(inv, line, f"line {number}")
    return inv


# --------------------------------------------------------------------------
# HTML helpers
# --------------------------------------------------------------------------

def label_lines(root: Node, skip: tuple[Node, ...] = ()) -> list[str]:
    """Text lines from 2-cell table rows, dt/dd pairs and paragraphs."""
    out: list[str] = []
    for table in root.find_all("table"):
        if any(table is s for s in skip):
            continue
        for tr in table.find_all("tr"):
            cells = row_cells(tr)
            if len(cells) == 2:
                out.append(f"{cells[0].rstrip(':')}: {cells[1]}")
            elif cells:
                out.append(" ".join(cells))
    for dl in root.find_all("dl"):
        label = None
        for child in dl.iter():
            if child.tag == "dt":
                label = child.text()
            elif child.tag == "dd" and label is not None:
                out.append(f"{label.rstrip(':')}: {child.text()}")
                label = None
    for p in root.find_all("p"):
        out.append(p.text())
    return [line for line in out if line.strip()]


# --------------------------------------------------------------------------
# Layout C: HTML with one or more line-items tables
# --------------------------------------------------------------------------

def _find_items_tables(root: Node) -> list[tuple[Node, list[str], dict[str, int], list[Node]]]:
    """Every table whose header row names a description, quantity, price and amount column."""
    found = []
    for table in root.find_all("table"):
        rows = table.find_all("tr")
        for i, tr in enumerate(rows):
            cells = [c for c in tr.elements() if c.tag in ("td", "th")]
            if not cells:
                continue
            is_header = tr.has_ancestor("thead") or all(c.tag == "th" for c in cells)
            if is_header:
                header = [c.text() for c in cells]
                columns = map_columns(header)
                if columns:
                    found.append((table, header, columns, rows[i + 1:]))
            break  # only the first non-empty row can be the header
    return found


def detect_html_table(doc: Document) -> bool:
    return doc.kind == "html" and bool(_find_items_tables(doc.dom))


def parse_html_table(doc: Document) -> Invoice:
    tables = _find_items_tables(doc.dom)
    inv = Invoice(source_file=doc.name, layout="html-table")
    others = label_lines(doc.dom, skip=tuple(t[0] for t in tables))
    inv.vendor = vendor_from_dom(doc.dom, others)
    apply_header(inv, header_fields(others))

    for number, (_, header, columns, rows) in enumerate(tables, start=1):
        prefix = "table row" if len(tables) == 1 else f"table {number} row"
        for index, tr in enumerate(rows, start=1):
            cells = row_cells(tr)
            if not any(cells):
                continue
            where = f"{prefix} {index}"
            qty_cell = cells[columns["quantity"]] if len(cells) > columns["quantity"] else ""
            looks_like_total = tr.has_ancestor("tfoot") or len(cells) != len(header) or not qty_cell
            if looks_like_total:
                total = classify_total_line(" ".join(cells))
                if total:
                    apply_total(inv, *total)
                else:
                    inv.warnings.append(f"{where}: could not read an item row: {cells}")
                continue
            item = item_from_cells(cells, columns, where, inv, index)
            if item:
                inv.line_items.append(item)

    # Totals printed outside the items tables (a separate totals block), and any
    # other amount on the page that is not a total.
    for line in others:
        read_totals_line(inv, line, "outside the items table")
    return inv


# --------------------------------------------------------------------------
# Layout D: HTML with "3 x Service @ $10.00 = $30.00" list items
# --------------------------------------------------------------------------

_LIST_ITEM = re.compile(
    r"^\s*(?P<qty>\d[\d,]*(?:\.\d+)?)\s*[x×]\s+(?P<desc>.+?)\s+@\s+"
    r"(?P<unit>\$?\s?[\d,]+\.\d{2,4})\s*(?:each\s*)?=\s*(?P<amount>\(?-?\$?\s?[\d,]+\.\d{2}\)?)\s*$",
    re.IGNORECASE,
)


def _item_lists(root: Node) -> list[Node]:
    lists = []
    for lst in root.find_all("ul") + root.find_all("ol"):
        items = [li for li in lst.elements() if li.tag == "li"]
        if any(_LIST_ITEM.match(li.text()) for li in items):
            lists.append(lst)
    return lists


def detect_html_list(doc: Document) -> bool:
    return doc.kind == "html" and bool(_item_lists(doc.dom))


def parse_html_list(doc: Document) -> Invoice:
    inv = Invoice(source_file=doc.name, layout="html-list")
    lines = label_lines(doc.dom)
    inv.vendor = vendor_from_dom(doc.dom, lines)
    apply_header(inv, header_fields(lines))

    index = 0
    for lst in _item_lists(doc.dom):
        for li in lst.elements():
            if li.tag != "li":
                continue
            index += 1
            text = li.text()
            m = _LIST_ITEM.match(text)
            if not m:
                inv.warnings.append(f"list item {index}: could not read an item row: {text!r}")
                continue
            inv.line_items.append(LineItem(
                clean(m["desc"]), parse_quantity(m["qty"]),
                parse_money(m["unit"]), parse_money(m["amount"]), index,
            ))

    totals_block = doc.dom.find(class_="total")
    source = totals_block if totals_block is not None else doc.dom
    for p in source.find_all("p"):
        read_totals_line(inv, p.text(), "totals")
    return inv


# --------------------------------------------------------------------------
# Fallback for anything else
# --------------------------------------------------------------------------

UNRECOGNISED_WARNING = "layout not recognised; fields were read by the generic fallback"


def parse_generic(doc: Document) -> Invoice:
    lines = doc.text.splitlines()
    inv = Invoice(source_file=doc.name, layout="unrecognised")
    inv.warnings.append(UNRECOGNISED_WARNING)
    inv.vendor = vendor_from_dom(doc.dom) if doc.dom is not None else vendor_from_lines(lines)
    apply_header(inv, header_fields(lines))
    for number, line in enumerate(lines, start=1):
        m = _LIST_ITEM.match(line)
        if m:
            inv.line_items.append(LineItem(
                clean(m["desc"]), parse_quantity(m["qty"]),
                parse_money(m["unit"]), parse_money(m["amount"]), number,
            ))
            continue
        m = _A_ROW.match(line)
        if m and not classify_total_line(line):
            inv.line_items.append(LineItem(
                clean(m["desc"]), parse_quantity(m["qty"]),
                parse_money(m["unit"]), parse_money(m["amount"]), number,
            ))
            continue
        total = classify_total_line(line)
        if total:
            apply_total(inv, *total)
    return inv


LAYOUTS = (
    ("text-pipe-table", detect_text_pipe_table, parse_text_pipe_table),
    ("text-columns", detect_text_columns, parse_text_columns),
    ("html-table", detect_html_table, parse_html_table),
    ("html-list", detect_html_list, parse_html_list),
)
_PARSERS = {name: parse for name, _, parse in LAYOUTS}


def detect_layout(doc: Document) -> str:
    """The name of the first layout whose detector accepts the document, or "unrecognised"."""
    for name, detect, _ in LAYOUTS:
        if detect(doc):
            return name
    return "unrecognised"


def extract(doc: Document) -> Invoice:
    parse = _PARSERS.get(detect_layout(doc), parse_generic)
    return parse(doc)
