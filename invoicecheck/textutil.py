"""Parsing helpers for money, quantities and dates as they appear on invoices.

Everything monetary is a Decimal. Floats never touch an amount, so a check
like "subtotal + tax == total" is exact arithmetic, not a float comparison.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

_MONEY_FULL = re.compile(
    rf"""^(?P<open>\()?\s*
        (?P<sign>-)?\s*
        (?:USD|US\$|\$)?\s*
        (?P<sign2>-)?\s*
        (?P<num>{_NUMBER})\s*
        (?:USD)?\s*
        (?P<close>\))?$""",
    re.IGNORECASE | re.VERBOSE,
)

# A money amount inside a longer line. Cents are required here so that
# quantities ("12") and rates ("7.25%") are not mistaken for amounts.
MONEY_TOKEN = re.compile(
    r"\(?-?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?(?![\d%])"
)

PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")

NUMBER_TOKEN = re.compile(_NUMBER)

_QUANTITY = re.compile(rf"^(?:{_NUMBER})$")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d %B, %Y",
    "%d %b, %Y",
)


def clean(text: str | None) -> str:
    """Collapse runs of whitespace and strip the ends."""
    return " ".join((text or "").split())


def parse_money(text: str | None) -> Decimal | None:
    """Parse a cell that should contain only an amount: "$1,240.00", "(12.50)", "-3.00".

    Returns None when the text is not an amount, rather than guessing.
    """
    if text is None:
        return None
    s = clean(text).replace("−", "-")
    if not s:
        return None
    m = _MONEY_FULL.match(s)
    if not m:
        return None
    if bool(m["open"]) != bool(m["close"]):
        return None
    try:
        value = Decimal(m["num"].replace(",", ""))
    except InvalidOperation:
        return None
    if m["open"] or m["sign"] or m["sign2"]:
        value = -value
    return value


def last_money(line: str) -> Decimal | None:
    """The right-most amount on a line, which is where totals print their value."""
    tokens = MONEY_TOKEN.findall(line)
    if not tokens:
        return None
    return parse_money(tokens[-1])


def parse_quantity(text: str | None) -> Decimal | None:
    s = clean(text)
    if not s or not _QUANTITY.match(s):
        return None
    return Decimal(s.replace(",", ""))


def parse_percent(text: str | None) -> Decimal | None:
    m = PERCENT.search(text or "")
    return Decimal(m.group(1)) if m else None


def parse_date(text: str | None) -> date | None:
    """Parse the date formats seen on the sample invoices.

    Slash dates are read as month/day/year (US invoices). Anything that does
    not parse returns None so the verifier can say so, instead of guessing.
    """
    s = clean(text)
    if not s:
        return None
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)
    s = re.sub(r"\bSept\b", "Sep", s, flags=re.IGNORECASE)
    s = s.strip(" .")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def cents(value: Decimal) -> Decimal:
    """Round to cents the way a till does (half up), not banker's rounding."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "(none)"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(cents(value)):,.2f}"


def fmt_number(value: Decimal | None) -> str:
    """A quantity or rate without trailing zeros or exponent notation."""
    if value is None:
        return ""
    return format(value.normalize(), "f")


def money_str(value: Decimal | None) -> str:
    """A plain two-decimal amount for CSV and JSON: no symbol, no separators."""
    if value is None:
        return ""
    return format(cents(value), "f")


def price_str(value: Decimal | None) -> str:
    """A unit price for CSV and JSON, as printed: at least two decimals, never rounded.

    Unit prices can go below a cent ($0.125 per GB). Rounding one to cents would
    write a number that is not on the invoice and no longer multiplies out to
    the line amount, so only the amount columns are rounded.
    """
    if value is None:
        return ""
    if value.as_tuple().exponent >= -2:
        return format(value.quantize(CENT), "f")   # pads 42.5 to 42.50; nothing is lost
    return format(value, "f")


def fmt_price(value: Decimal | None) -> str:
    """A unit price for messages: "$1,042.50", "$0.125"."""
    if value is None:
        return "(none)"
    sign = "-" if value < 0 else ""
    whole, _, frac = price_str(abs(value)).partition(".")
    return f"{sign}${int(whole):,}.{frac}"


def numbers_in(text: str) -> set[Decimal]:
    """Every number printed in a document, as Decimals (1,240.00 == 1240)."""
    found = set()
    for token in NUMBER_TOKEN.findall(text or ""):
        try:
            found.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def alnum_upper(text: str | None) -> str:
    """Uppercase letters and digits only: "mg 5512" and "MG-5512" compare equal."""
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
