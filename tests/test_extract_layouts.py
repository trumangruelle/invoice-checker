"""One test class per extractor layout, plus detection and the generic fallback."""

import unittest
from datetime import date
from decimal import Decimal

from invoicecheck.documents import document_from_string
from invoicecheck.extract_rules import (
    UNRECOGNISED_WARNING,
    classify_total_line,
    detect_layout,
    extract,
    header_fields,
    is_banner,
    map_columns,
    vendor_from_dom,
)
from invoicecheck.htmldoc import parse_html

from .helpers import fixture_doc, sample

BANNERS = ("PAYMENT REMINDER - SECOND NOTICE", "COPY", "Page 1 of 2", "*** DUPLICATE ***",
           "Payment reminder: this invoice is now overdue.")


def amounts(inv):
    return [item.amount for item in inv.line_items]


def edited(name, old, new):
    """A sample with one edit applied, loaded as a new document. Fails loudly if the edit misses."""
    raw = sample(name).raw
    if raw.count(old) != 1:
        raise AssertionError(f"edit target found {raw.count(old)} times in {name}: {old!r}")
    return extract(document_from_string(name, raw.replace(old, new)))


class TextColumnsLayoutTests(unittest.TestCase):
    """Layout A: plain text, whitespace-aligned columns (Bramblewood samples)."""

    def setUp(self):
        self.inv = extract(sample("bramblewood_BP-10421.txt"))

    def test_header_fields(self):
        self.assertEqual(self.inv.layout, "text-columns")
        self.assertEqual(self.inv.vendor, "Bramblewood Paper Co.")
        self.assertEqual(self.inv.invoice_number, "BP-10421")
        self.assertEqual(self.inv.invoice_date, date(2026, 8, 3))
        self.assertEqual(self.inv.due_date, date(2026, 9, 2))

    def test_line_items_and_totals(self):
        self.assertEqual(len(self.inv.line_items), 4)
        first = self.inv.line_items[0]
        self.assertEqual((first.description, first.quantity, first.unit_price, first.amount),
                         ("Copy paper, letter, 10-ream case", Decimal("6"), Decimal("42.50"), Decimal("255.00")))
        self.assertEqual(self.inv.subtotal, Decimal("406.30"))
        self.assertEqual(self.inv.tax, Decimal("29.46"))
        self.assertEqual(self.inv.tax_rate, Decimal("7.25"))
        self.assertEqual(self.inv.total, Decimal("435.76"))
        self.assertEqual(self.inv.warnings, [])

    def test_wrapped_description_joins_the_previous_item(self):
        self.assertEqual(self.inv.line_items[2].description,
                         "Heavyweight cardstock, 250 sheets (white, 110 lb)")

    def test_thousands_separators(self):
        inv = extract(sample("bramblewood_BP-10437.txt"))
        self.assertEqual(inv.subtotal, Decimal("1021.40"))
        self.assertEqual(inv.total, Decimal("1095.45"))

    def test_unreadable_row_becomes_a_warning_not_a_silent_drop(self):
        text = (sample("bramblewood_BP-10421.txt").text
                .replace("Delivery                               1        15.00       15.00",
                         "Delivery  one  15.00  15.00"))
        inv = extract(document_from_string("broken.txt", text))
        self.assertEqual(len(inv.line_items), 3)
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("could not read an item row", inv.warnings[0])
        self.assertIn("Delivery", inv.warnings[0])

    def test_banner_line_above_the_vendor_is_not_the_vendor(self):
        for banner in BANNERS:
            with self.subTest(banner=banner):
                inv = edited("bramblewood_BP-10421.txt", "Bramblewood Paper Co.\n",
                             f"{banner}\nBramblewood Paper Co.\n")
                self.assertEqual(inv.vendor, "Bramblewood Paper Co.")
                self.assertEqual(inv.warnings, [])

    def test_labelled_vendor_wins_over_the_first_line(self):
        inv = edited("bramblewood_BP-10421.txt", "Bramblewood Paper Co.\n",
                     "Re: August supplies\nSupplier: Bramblewood Paper Co.\n")
        self.assertEqual(inv.vendor, "Bramblewood Paper Co.")

    def test_item_whose_description_mentions_tax_or_total_is_an_item(self):
        for desc in ("Tax filing folders, box of 500", "Total-care desk wipes, box of 500"):
            with self.subTest(desc=desc):
                inv = edited("bramblewood_BP-10421.txt", "Recycled kraft envelopes, box of 500   2",
                             f"{desc:<36}   2")
                self.assertEqual(len(inv.line_items), 4)
                self.assertEqual(inv.line_items[1].description, desc)
                self.assertEqual((inv.tax, inv.total), (Decimal("29.46"), Decimal("435.76")))
                self.assertEqual(inv.warnings, [])

    def test_item_row_without_cents_is_a_warning_not_part_of_a_description(self):
        inv = edited("bramblewood_BP-10421.txt",
                     "Delivery                               1        15.00       15.00",
                     "Delivery                               1        15          15")
        self.assertEqual(len(inv.line_items), 3)
        self.assertEqual(inv.line_items[2].description, "Heavyweight cardstock, 250 sheets (white, 110 lb)")
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("line 19: could not read an item row: 'Delivery 1 15 15'", inv.warnings[0])

    def test_unlabelled_amount_in_the_totals_is_a_warning(self):
        inv = edited("bramblewood_BP-10421.txt", "Subtotal:        406.30\n",
                     "Subtotal:        406.30\n                                          Shipping:         12.00\n")
        self.assertEqual(inv.subtotal, Decimal("406.30"))
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("'Shipping: 12.00' is an amount that is not a subtotal, tax or total", inv.warnings[0])

    def test_item_row_below_the_totals_is_a_warning(self):
        inv = edited("bramblewood_BP-10421.txt", "Payment terms:",
                     "Late delivery credit                   1        15.00       15.00\nPayment terms:")
        self.assertEqual(len(inv.line_items), 4)
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("an item row after the totals was not read", inv.warnings[0])

    def test_date_of_service_does_not_hide_the_invoice_date(self):
        inv = edited("bramblewood_BP-10421.txt", "Invoice Number:", "Date of service:  2026-07-30\nInvoice Number:")
        self.assertEqual(inv.invoice_date, date(2026, 8, 3))
        self.assertEqual(inv.warnings, [])


class TextPipeTableLayoutTests(unittest.TestCase):
    """Layout B: plain text with a pipe table (Kestrel samples)."""

    def setUp(self):
        self.inv = extract(sample("kestrel_KR-2291.txt"))

    def test_header_fields_with_us_dates(self):
        self.assertEqual(self.inv.layout, "text-pipe-table")
        self.assertEqual(self.inv.vendor, "Kestrelrook IT Services")
        self.assertEqual(self.inv.invoice_number, "KR-2291")
        self.assertEqual(self.inv.invoice_date, date(2026, 8, 11))
        self.assertEqual(self.inv.due_date, date(2026, 9, 10))

    def test_columns_are_mapped_by_header_not_position(self):
        # The SKU column comes first; description must still be the Item column.
        self.assertEqual(self.inv.line_items[0].description, "Managed support, per seat")
        self.assertEqual(self.inv.line_items[3].quantity, Decimal("2.5"))
        self.assertEqual(amounts(self.inv), [Decimal("490.00"), Decimal("39.98"),
                                             Decimal("72.00"), Decimal("237.50")])

    def test_totals(self):
        self.assertEqual(self.inv.subtotal, Decimal("839.48"))
        self.assertEqual(self.inv.tax, Decimal("50.37"))
        self.assertEqual(self.inv.tax_rate, Decimal("6"))
        self.assertEqual(self.inv.total, Decimal("889.85"))
        self.assertEqual(self.inv.warnings, [])

    def test_row_with_wrong_cell_count_is_a_warning(self):
        text = sample("kestrel_KR-2291.txt").text.replace(
            "| HW-310 | Replacement keyboard       | 3   | $24.00  | $72.00     |",
            "| HW-310 | Replacement keyboard       | 3   | $24.00  |")
        inv = extract(document_from_string("broken.txt", text))
        self.assertEqual(len(inv.line_items), 3)
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("Replacement keyboard", inv.warnings[0])

    def test_banner_line_above_the_vendor_is_not_the_vendor(self):
        for banner in BANNERS:
            with self.subTest(banner=banner):
                inv = edited("kestrel_KR-2291.txt", "Kestrelrook IT Services\n",
                             f"{banner}\nKestrelrook IT Services\n")
                self.assertEqual(inv.vendor, "Kestrelrook IT Services")
                self.assertEqual(inv.warnings, [])

    def test_sub_cent_unit_price_is_kept_as_printed(self):
        inv = edited("kestrel_KR-2291.txt",
                     "| BK-020 | Cloud backup, 500 GB       | 2   | $19.99  | $39.98     |",
                     "| BK-020 | Cloud backup, per GB       | 320 | $0.125  | $40.00     |")
        item = inv.line_items[1]
        self.assertEqual((item.quantity, item.unit_price, item.amount),
                         (Decimal("320"), Decimal("0.125"), Decimal("40.00")))
        self.assertEqual(item.to_dict()["unit_price"], "0.125")

    def test_unlabelled_amount_below_the_table_is_a_warning(self):
        inv = edited("kestrel_KR-2291.txt", "SUBTOTAL:              $839.48\n",
                     "SUBTOTAL:              $839.48\nFREIGHT:                $18.00\n")
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("'FREIGHT: $18.00' is an amount that is not a subtotal, tax or total", inv.warnings[0])


class HtmlTableLayoutTests(unittest.TestCase):
    """Layout C: HTML with a line-items table (Lanternfish samples)."""

    def setUp(self):
        self.inv = extract(sample("lanternfish_LF-0087.html"))

    def test_header_fields_from_meta_table(self):
        self.assertEqual(self.inv.layout, "html-table")
        self.assertEqual(self.inv.vendor, "Lanternfish Catering LLC")
        self.assertEqual(self.inv.invoice_number, "LF-0087")
        self.assertEqual(self.inv.invoice_date, date(2026, 8, 20))
        self.assertEqual(self.inv.due_date, date(2026, 9, 19))

    def test_items_and_tfoot_totals(self):
        self.assertEqual(len(self.inv.line_items), 4)
        self.assertEqual(self.inv.line_items[1].description, "Coffee & tea service, per guest")
        self.assertEqual(self.inv.subtotal, Decimal("1240.00"))
        self.assertEqual(self.inv.tax, Decimal("110.05"))
        self.assertEqual(self.inv.tax_rate, Decimal("8.875"))
        self.assertEqual(self.inv.total, Decimal("1350.05"))
        self.assertEqual(self.inv.warnings, [])

    def test_totals_outside_the_table_are_found(self):
        html = """<html><body><h1>Test Vendor</h1>
        <table><tr><th>Invoice No.</th><td>X-1</td></tr><tr><th>Date</th><td>2026-09-01</td></tr></table>
        <table><thead><tr><th>Description</th><th>Qty</th><th>Unit Price</th><th>Amount</th></tr></thead>
        <tbody><tr><td>Thing</td><td>2</td><td>$5.00</td><td>$10.00</td></tr></tbody></table>
        <div class="summary"><p>Subtotal: $10.00</p><p>Total: $10.00</p></div></body></html>"""
        inv = extract(document_from_string("t.html", html))
        self.assertEqual(inv.layout, "html-table")
        self.assertEqual((inv.subtotal, inv.total), (Decimal("10.00"), Decimal("10.00")))
        self.assertEqual(inv.invoice_number, "X-1")

    def test_unreadable_cell_becomes_a_warning(self):
        html = sample("lanternfish_LF-0087.html").raw.replace(
            "<td>Dessert platter</td><td>5</td>", "<td>Dessert platter</td><td>five</td>")
        inv = extract(document_from_string("t.html", html))
        self.assertEqual(len(inv.line_items), 3)
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("Dessert platter", inv.warnings[0])

    def test_body_row_with_an_empty_quantity_that_is_not_a_total_is_a_warning(self):
        inv = edited("lanternfish_LF-0087.html", "<td>Dessert platter</td><td>5</td>",
                     "<td>Dessert platter</td><td></td>")
        self.assertEqual(len(inv.line_items), 3)
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("table row 3: could not read an item row", inv.warnings[0])
        self.assertIn("Dessert platter", inv.warnings[0])

    def test_every_items_table_is_read(self):
        inv = edited("lanternfish_LF-0087.html", '  <p class="terms">',
                     "  <table><thead><tr><th>Item</th><th>Quantity</th><th>Price</th><th>Total</th></tr></thead>\n"
                     "  <tbody><tr><td>Chafing dish hire</td><td>2</td><td>$10.00</td><td>$20.00</td></tr></tbody>"
                     "</table>\n  <p class=\"terms\">")
        self.assertEqual(len(inv.line_items), 5)
        self.assertEqual((inv.line_items[4].description, inv.line_items[4].amount),
                         ("Chafing dish hire", Decimal("20.00")))
        self.assertEqual(inv.warnings, [])

    def test_amount_outside_the_table_that_is_not_a_total_is_a_warning(self):
        inv = edited("lanternfish_LF-0087.html", '  <p class="terms">',
                     '  <p>Deposit received: $200.00</p>\n  <p class="terms">')
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("'Deposit received: $200.00' is an amount that is not a subtotal", inv.warnings[0])

    def test_banner_heading_is_not_the_vendor(self):
        inv = edited("lanternfish_LF-0087.html", '<h1 class="vendor-name">Lanternfish Catering LLC</h1>',
                     "<h1>TAX INVOICE</h1>\n    <h2>Lanternfish Catering LLC</h2>")
        self.assertEqual(inv.vendor, "Lanternfish Catering LLC")


class HtmlListLayoutTests(unittest.TestCase):
    """Layout D: HTML with "qty x item @ price = amount" list items (Mossgate samples)."""

    def setUp(self):
        self.inv = extract(sample("mossgate_MG-5512.html"))

    def test_header_fields_from_definition_list(self):
        self.assertEqual(self.inv.layout, "html-list")
        self.assertEqual(self.inv.vendor, "Mossgate Janitorial Services")
        self.assertEqual(self.inv.invoice_number, "MG-5512")
        self.assertEqual(self.inv.invoice_date, date(2026, 8, 14))
        self.assertEqual(self.inv.due_date, date(2026, 8, 28))

    def test_items_and_totals(self):
        self.assertEqual([i.description for i in self.inv.line_items],
                         ["Nightly office clean (weekly block)", "Carpet shampoo, reception",
                          "Restroom supplies restock"])
        self.assertEqual(self.inv.line_items[2].quantity, Decimal("6"))
        self.assertEqual(self.inv.subtotal, Decimal("1140.00"))
        self.assertEqual(self.inv.tax, Decimal("57.00"))
        self.assertEqual(self.inv.tax_rate, Decimal("5"))
        self.assertEqual(self.inv.total, Decimal("1197.00"))
        self.assertEqual(self.inv.warnings, [])

    def test_reminder_banner_does_not_disturb_extraction(self):
        # The sample has an <h1>PAYMENT REMINDER - SECOND NOTICE</h1> above the vendor's <h2>.
        inv = extract(sample("mossgate_MG-5512_reminder.html"))
        self.assertEqual((inv.vendor, inv.invoice_number, inv.total, len(inv.line_items)),
                         ("Mossgate Janitorial Services", "MG-5512", Decimal("1197.00"), 3))
        self.assertEqual(inv.warnings, [])

    def test_banner_heading_is_not_the_vendor(self):
        for banner in BANNERS:
            with self.subTest(banner=banner):
                inv = edited("mossgate_MG-5512.html", "<header>\n", f"<header>\n  <h1>{banner}</h1>\n")
                self.assertEqual(inv.vendor, "Mossgate Janitorial Services")

    def test_unlabelled_amount_in_the_totals_is_a_warning(self):
        inv = edited("mossgate_MG-5512.html", "  <p>Tax 5%: $57.00</p>",
                     "  <p>Fuel surcharge: $12.00</p>\n  <p>Tax 5%: $57.00</p>")
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("totals: 'Fuel surcharge: $12.00' is an amount", inv.warnings[0])

    def test_unreadable_list_item_becomes_a_warning(self):
        html = sample("mossgate_MG-5512.html").raw.replace(
            "<li>1 x Carpet shampoo, reception @ $165.00 = $165.00</li>",
            "<li>Carpet shampoo, reception, one visit: $165.00</li>")
        inv = extract(document_from_string("t.html", html))
        self.assertEqual(len(inv.line_items), 2)
        self.assertEqual(len(inv.warnings), 1)
        self.assertIn("Carpet shampoo", inv.warnings[0])


class DetectionAndFallbackTests(unittest.TestCase):
    def test_each_sample_routes_to_its_layout(self):
        expected = {
            "bramblewood_BP-10421.txt": "text-columns",
            "kestrel_KR-2291.txt": "text-pipe-table",
            "lanternfish_LF-0087.html": "html-table",
            "mossgate_MG-5512.html": "html-list",
        }
        for name, layout in expected.items():
            with self.subTest(name=name):
                self.assertEqual(detect_layout(sample(name)), layout)

    def test_unrecognised_layout_uses_generic_fallback_and_says_so(self):
        inv = extract(fixture_doc("glasswing_email_invoice.txt"))
        self.assertEqual(inv.layout, "unrecognised")
        self.assertIn(UNRECOGNISED_WARNING, inv.warnings)
        self.assertEqual(inv.total, Decimal("216.00"))

    def test_header_label_variants(self):
        fields = header_fields(["INVOICE #: KR-1", "Issued: 14 Aug 2026", "Payment due: 28 Aug 2026"])
        self.assertEqual(fields, {"invoice_number": "KR-1", "invoice_date": "14 Aug 2026",
                                  "due_date": "28 Aug 2026"})

    def test_label_must_be_a_whole_word(self):
        self.assertEqual(header_fields(["Dates of service: August"]), {})

    def test_specific_date_label_beats_a_bare_date_above_it(self):
        fields = header_fields(["Date of service:  2026-07-30", "Invoice Number:   BP-1",
                                "Invoice Date:     2026-08-03"])
        self.assertEqual(fields, {"invoice_number": "BP-1", "invoice_date": "2026-08-03"})

    def test_bare_date_needs_a_separator_or_a_date_after_it(self):
        self.assertEqual(header_fields(["Date of service: 2026-07-30", "Date: 2026-08-03"]),
                         {"invoice_date": "2026-08-03"})
        self.assertEqual(header_fields(["Date 14 Aug 2026"]), {"invoice_date": "14 Aug 2026"})
        self.assertEqual(header_fields(["Date   August 20, 2026"]), {"invoice_date": "August 20, 2026"})
        self.assertEqual(header_fields(["Date", "2026-08-03"]), {"invoice_date": "2026-08-03"})

    def test_due_to_is_not_a_due_date(self):
        self.assertEqual(header_fields(["Due to the holiday, delivery was late."]), {})
        self.assertEqual(header_fields(["Due: 2026-10-01"]), {"due_date": "2026-10-01"})

    def test_banner_lines(self):
        for line in BANNERS + ("INVOICE", "Tax Invoice", "Customer copy", "-----"):
            with self.subTest(line=line):
                self.assertTrue(is_banner(line))
        for line in ("Bramblewood Paper Co.", "Final Notice Printing LLC", "Statement Stationers",
                     "Re: August supplies", "418 Fernhollow Road, Quillmere"):
            with self.subTest(line=line):
                self.assertFalse(is_banner(line))

    def test_vendor_from_dom(self):
        block = '<div class="vendor"><p>Quillfeather Printing</p><p>12 Inkpot Lane</p></div>'
        self.assertEqual(vendor_from_dom(parse_html(block)), "Quillfeather Printing")
        # "company-address" is not a vendor class; the heading is used instead.
        other = '<p class="company-address">12 Inkpot Lane</p><h1>COPY</h1><h1>Quillfeather Printing</h1>'
        self.assertEqual(vendor_from_dom(parse_html(other)), "Quillfeather Printing")
        labelled = '<h1>Statement</h1><dl><dt>Vendor</dt><dd>Quillfeather Printing</dd></dl>'
        self.assertEqual(vendor_from_dom(parse_html(labelled)), "Quillfeather Printing")

    def test_label_alone_takes_value_from_next_line(self):
        self.assertEqual(header_fields(["Invoice No.", "Z-9"]), {"invoice_number": "Z-9"})

    def test_classify_total_line(self):
        self.assertEqual(classify_total_line("Subtotal: 10.00"), ("subtotal", Decimal("10.00"), None))
        self.assertEqual(classify_total_line("Sales tax (8.875%) $110.05"),
                         ("tax", Decimal("110.05"), Decimal("8.875")))
        self.assertEqual(classify_total_line("AMOUNT DUE: $889.85"), ("total", Decimal("889.85"), None))
        self.assertIsNone(classify_total_line("Thank you for your business"))

    def test_map_columns_requires_all_four(self):
        self.assertIsNotNone(map_columns(["SKU", "Item", "Qty", "Rate", "Line Total"]))
        self.assertIsNone(map_columns(["Item", "Qty", "Total"]))


if __name__ == "__main__":
    unittest.main()
