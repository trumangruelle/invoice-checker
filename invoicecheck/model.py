"""The extracted-invoice schema shared by the rules extractor and the AI extractor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .textutil import fmt_number, money_str, price_str


@dataclass
class LineItem:
    description: str
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    source_line: int | None = None   # line in the file (text) or row in the table (HTML)

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "quantity": fmt_number(self.quantity) if self.quantity is not None else None,
            "unit_price": price_str(self.unit_price) if self.unit_price is not None else None,
            "amount": money_str(self.amount) if self.amount is not None else None,
            "source_line": self.source_line,
        }


@dataclass
class Invoice:
    source_file: str
    layout: str = "unrecognised"
    extractor: str = "rules"
    vendor: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    invoice_date_raw: str | None = None
    due_date: date | None = None
    due_date_raw: str | None = None
    currency: str = "USD"
    line_items: list[LineItem] = field(default_factory=list)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    tax_rate: Decimal | None = None   # percent, e.g. Decimal("7.25")
    total: Decimal | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        def money(v):
            return money_str(v) if v is not None else None

        return {
            "source_file": self.source_file,
            "layout": self.layout,
            "extractor": self.extractor,
            "vendor": self.vendor,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "currency": self.currency,
            "line_items": [item.to_dict() for item in self.line_items],
            "subtotal": money(self.subtotal),
            "tax": money(self.tax),
            "tax_rate_percent": fmt_number(self.tax_rate) if self.tax_rate is not None else None,
            "total": money(self.total),
            "warnings": list(self.warnings),
        }
