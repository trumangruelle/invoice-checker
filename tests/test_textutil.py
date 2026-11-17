import unittest
from datetime import date
from decimal import Decimal

from invoicecheck.textutil import (
    alnum_upper,
    cents,
    fmt_money,
    fmt_price,
    last_money,
    numbers_in,
    parse_date,
    parse_money,
    parse_percent,
    parse_quantity,
    price_str,
)


class ParseMoneyTests(unittest.TestCase):
    def test_plain_and_symbol_and_separators(self):
        self.assertEqual(parse_money("406.30"), Decimal("406.30"))
        self.assertEqual(parse_money("$1,240.00"), Decimal("1240.00"))
        self.assertEqual(parse_money(" USD 12.5 "), Decimal("12.5"))
        self.assertEqual(parse_money("$840"), Decimal("840"))

    def test_negative_forms(self):
        self.assertEqual(parse_money("(12.00)"), Decimal("-12.00"))
        self.assertEqual(parse_money("-$3.00"), Decimal("-3.00"))
        self.assertEqual(parse_money("$-3.00"), Decimal("-3.00"))
        self.assertEqual(parse_money("−5.00"), Decimal("-5.00"))

    def test_not_money_returns_none(self):
        for text in ("", None, "abc", "12 boxes", "(12.00", "1.2.3", "$"):
            with self.subTest(text=text):
                self.assertIsNone(parse_money(text))


class LineScanningTests(unittest.TestCase):
    def test_last_money_ignores_rates_and_quantities(self):
        self.assertEqual(last_money("Tax (7.25%):      29.46"), Decimal("29.46"))
        self.assertEqual(last_money("SALES TAX (6%):  $50.37"), Decimal("50.37"))
        self.assertIsNone(last_money("Tax 5%"))
        self.assertIsNone(last_money("Qty 12"))

    def test_percent(self):
        self.assertEqual(parse_percent("Sales tax (8.875%)"), Decimal("8.875"))
        self.assertIsNone(parse_percent("Sales tax"))

    def test_quantity(self):
        self.assertEqual(parse_quantity("2.5"), Decimal("2.5"))
        self.assertEqual(parse_quantity("1,200"), Decimal("1200"))
        self.assertIsNone(parse_quantity("three"))
        self.assertIsNone(parse_quantity("2 hrs"))


class ParseDateTests(unittest.TestCase):
    def test_formats_used_by_the_samples(self):
        self.assertEqual(parse_date("2026-08-03"), date(2026, 8, 3))
        self.assertEqual(parse_date("08/11/2026"), date(2026, 8, 11))
        self.assertEqual(parse_date("August 20, 2026"), date(2026, 8, 20))
        self.assertEqual(parse_date("14 Aug 2026"), date(2026, 8, 14))

    def test_other_common_forms(self):
        self.assertEqual(parse_date("Sept 3, 2026"), date(2026, 9, 3))
        self.assertEqual(parse_date("3rd September 2026"), date(2026, 9, 3))
        self.assertEqual(parse_date("2026/09/03"), date(2026, 9, 3))

    def test_slash_dates_are_month_first(self):
        self.assertEqual(parse_date("03/04/2026"), date(2026, 3, 4))

    def test_unreadable_returns_none(self):
        for text in ("", None, "soon", "31/31/2026", "2026-13-01"):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text))


class FormattingTests(unittest.TestCase):
    def test_cents_rounds_half_up(self):
        self.assertEqual(cents(Decimal("29.45675")), Decimal("29.46"))
        self.assertEqual(cents(Decimal("0.125")), Decimal("0.13"))  # banker's rounding would give 0.12

    def test_fmt_money(self):
        self.assertEqual(fmt_money(Decimal("1095.45")), "$1,095.45")
        self.assertEqual(fmt_money(Decimal("-5")), "-$5.00")
        self.assertEqual(fmt_money(None), "(none)")

    def test_price_str_keeps_every_printed_digit(self):
        self.assertEqual(price_str(Decimal("0.125")), "0.125")
        self.assertEqual(price_str(Decimal("0.004")), "0.004")
        self.assertEqual(price_str(Decimal("42.50")), "42.50")
        self.assertEqual(price_str(Decimal("42.5")), "42.50")
        self.assertEqual(price_str(Decimal("840")), "840.00")
        self.assertEqual(price_str(None), "")

    def test_fmt_price(self):
        self.assertEqual(fmt_price(Decimal("0.125")), "$0.125")
        self.assertEqual(fmt_price(Decimal("1042.5")), "$1,042.50")
        self.assertEqual(fmt_price(Decimal("-3.005")), "-$3.005")

    def test_numbers_in_normalises_separators(self):
        found = numbers_in("Subtotal $1,240.00 and 45 guests")
        self.assertIn(Decimal("1240"), found)
        self.assertIn(Decimal("45"), found)

    def test_alnum_upper(self):
        self.assertEqual(alnum_upper("mg 5512"), alnum_upper("MG-5512"))


if __name__ == "__main__":
    unittest.main()
