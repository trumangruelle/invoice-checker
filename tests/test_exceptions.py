"""The exceptions spreadsheet, written by the real command line.

One row per flag, with the reason in plain English and the money at stake.
Every test here drives `python -m invoicecheck run` through main().
"""

import csv
import io
import json
import unittest

from invoicecheck.cli import EXIT_OK
from invoicecheck.outputs import EXCEPTION_COLUMNS

from .helpers import CliCase, FakeOpener

# An invoice whose vendor and description are spreadsheet formulas. A person opening
# the exceptions list must not run them. Its total is $5.00 more than its subtotal,
# so it is held and therefore appears on the list.
HOSTILE = """=cmd|'/c calc'!A1
Small office support
=================================================
INVOICE #: HX-9001
Invoice Date: 2026-09-01
Payment Due: 2026-10-01
=================================================

| SKU    | Item                       | Qty | Rate    | Line Total |
|--------|----------------------------|-----|---------|------------|
| MS-100 | +SUM(A1:A9)                | 2   | $10.00  | $20.00     |

SUBTOTAL:               $20.00
AMOUNT DUE:             $25.00
"""

# A vendor name holding a comma, a double quote and a line break: the three things
# that break a hand-rolled CSV writer.
AWKWARD = '''Quill "and" Vellum, Ltd.
Stationers
=================================================
INVOICE #: QV-5
Invoice Date: 2026-09-01
=================================================

| SKU | Item                                   | Qty | Rate    | Line Total |
|-----|----------------------------------------|-----|---------|------------|
| A-1 | Ream, "letter" size, 20 lb, white      | 2   | $10.00  | $25.00     |

SUBTOTAL:               $25.00
AMOUNT DUE:             $25.00
'''


# A credit note: its total is negative, so total_not_positive holds it for a person.
CREDIT_NOTE = """Quillmere Supplies
Stationers
=================================================
INVOICE #: CN-7001
Invoice Date: 2026-09-01
=================================================

| SKU | Item                        | Qty | Rate     | Line Total |
|-----|-----------------------------|-----|----------|------------|
| R-1 | Credit, returned paper      | 1   | ($25.00) | ($25.00)   |

SUBTOTAL:               ($25.00)
AMOUNT DUE:             ($25.00)
"""


class ExceptionsSheetTests(CliCase):
    def exceptions(self) -> list[dict]:
        return self.read_csv("exceptions.csv")

    def test_one_row_per_flag_with_its_own_reason_and_money(self):
        # KR-2310 is held for two reasons, so it gets two rows, each with its own reason.
        self.copy_samples("kestrel_KR-2304.txt", "kestrel_KR-2310.txt", "bramblewood_BP-10421.txt")
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, EXIT_OK)
        rows = self.exceptions()
        self.assertEqual(list(rows[0]), EXCEPTION_COLUMNS)
        self.assertEqual([(r["SourceFile"], r["Check"]) for r in rows], [
            ("kestrel_KR-2304.txt", "line_math"),
            ("kestrel_KR-2310.txt", "date_future"),
            ("kestrel_KR-2310.txt", "due_before_issue"),
        ])
        # The invoice that passed is not an exception.
        self.assertNotIn("bramblewood_BP-10421.txt", [r["SourceFile"] for r in rows])
        line_math = rows[0]
        self.assertEqual((line_math["Status"], line_math["Vendor"], line_math["InvoiceNumber"],
                          line_math["InvoiceTotal"], line_math["Flag"]),
                         ("NEEDS REVIEW", "Kestrelrook IT Services", "KR-2304", "633.34",
                          "Line total is wrong"))
        self.assertIn("12 x $35.00 = $420.00, but the line shows $435.00", line_math["Reason"])
        self.assertEqual((line_math["FindingDifference"], line_math["DifferenceKind"]),
                         ("15.00", "overbilling"))
        self.assertEqual((line_math["InvoiceOverbilled"], line_math["InvoiceSentTwice"]),
                         ("15.00", "0.00"))
        # A flag that is not about an amount leaves the money columns empty rather than "0.00".
        self.assertEqual((rows[1]["FindingDifference"], rows[1]["DifferenceKind"]), ("", ""))
        self.assertIn("exceptions.csv (3 rows, one per flag)", stdout)

    def test_a_tax_shortfall_is_marked_an_undercharge_not_overbilling(self):
        self.copy_samples("lanternfish_LF-0091.html")
        self.run_cli()
        row = self.exceptions()[0]
        self.assertEqual((row["Check"], row["FindingDifference"], row["DifferenceKind"]),
                         ("tax_rate", "5.96", "undercharge"))
        # The invoice's counted overbilling is zero: the difference is in the buyer's favour.
        self.assertEqual(row["InvoiceOverbilled"], "0.00")

    def test_the_second_copy_of_an_invoice_carries_its_whole_total(self):
        self.copy_samples("mossgate_MG-5512.html", "mossgate_MG-5512_reminder.html")
        self.run_cli()
        rows = {r["SourceFile"]: r for r in self.exceptions()}
        self.assertEqual((rows["mossgate_MG-5512.html"]["FindingDifference"],
                          rows["mossgate_MG-5512.html"]["DifferenceKind"]), ("", ""))
        copy = rows["mossgate_MG-5512_reminder.html"]
        self.assertEqual((copy["FindingDifference"], copy["DifferenceKind"], copy["InvoiceSentTwice"]),
                         ("1197.00", "sent twice", "1197.00"))

    def test_a_file_that_could_not_be_read_is_on_the_list_too(self):
        self.copy_samples("bramblewood_BP-10421.txt", "kestrel_KR-2304.txt")
        (self.input / "receipt_photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        self.run_cli()
        rows = self.exceptions()
        self.assertEqual([(r["SourceFile"], r["Status"], r["Check"], r["Flag"]) for r in rows],
                         [("kestrel_KR-2304.txt", "NEEDS REVIEW", "line_math", "Line total is wrong"),
                          ("receipt_photo.jpg", "NOT CHECKED", "not_checked", "not checked")])
        self.assertEqual(rows[1]["Reason"], "image, not read: it needs OCR first")
        # Nothing was checked in it, so it has no invoice figures.
        self.assertEqual([rows[1][name] for name in ("Vendor", "InvoiceTotal", "InvoiceOverbilled")],
                         ["", "", ""])
        # Every row is the full width, the not-checked one included: a column added to
        # EXCEPTION_COLUMNS without padding that row would shift the ones after it.
        self.assertEqual({len(row) for row in self.read_rows("exceptions.csv")},
                         {len(EXCEPTION_COLUMNS)})

    def test_formulas_in_invoice_text_cannot_run_when_the_sheet_is_opened(self):
        self.write_invoice("hostile.txt", HOSTILE)
        self.run_cli()
        rows = self.exceptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Check"], "total_math")
        # The vendor came off the invoice as "=cmd|...": it is written as text, not a formula.
        self.assertEqual(rows[0]["Vendor"], "'=cmd|'/c calc'!A1")
        for row in self.read_rows("exceptions.csv"):
            for cell in row:
                self.assertFalse(cell.startswith(("=", "+", "-", "@", "\t", "\r")),
                                 f"{cell!r} would be read as a formula")
        # The same for the other two CSVs the same run writes.
        for name in ("import_ready.csv", "all_invoices.csv"):
            for row in self.read_rows(name):
                for cell in row:
                    self.assertFalse(cell.startswith(("=", "+", "-", "@", "\t", "\r")),
                                     f"{name}: {cell!r} would be read as a formula")

    def test_amounts_stay_numbers_a_spreadsheet_can_add_up(self):
        # A credit note's total is negative, and "-" is one of the characters a spreadsheet
        # reads as the start of a formula. Amounts are still written as plain numbers:
        # escaping them would turn the money columns into text nobody can total.
        self.write_invoice("credit.txt", CREDIT_NOTE)
        self.copy_samples("lanternfish_LF-0091.html")
        self.run_cli()
        rows = {r["SourceFile"]: r for r in self.exceptions()}
        self.assertEqual(rows["credit.txt"]["Check"], "total_not_positive")
        self.assertEqual(float(rows["credit.txt"]["InvoiceTotal"]), -25.00)
        self.assertEqual(float(rows["lanternfish_LF-0091.html"]["InvoiceTotal"]), 735.48)
        self.assertEqual(float(rows["lanternfish_LF-0091.html"]["FindingDifference"]), 5.96)

    def test_a_currency_the_ai_read_off_the_page_cannot_be_a_formula(self):
        # Currency is the one import column that is not a constant: --extractor ai takes
        # it from the document, so it is invoice text and has to be neutralised too.
        self.copy_samples("bramblewood_BP-10421.txt")
        reply = {
            "vendor": "Bramblewood Paper Co.", "invoice_number": "BP-10421",
            "invoice_date": "2026-08-03", "due_date": "2026-09-02", "currency": "=1+1",
            "line_items": [{"description": "Copy paper, letter, 10-ream case",
                            "quantity": "6", "unit_price": "42.50", "amount": "255.00"}],
            "subtotal": "255.00", "tax": None, "tax_rate_percent": None, "total": "255.00",
        }
        response = {"content": [{"type": "text", "text": json.dumps(reply)}],
                    "stop_reason": "end_turn"}
        code, _, stderr = self.cli("run", "--input", self.input, "--out", self.out,
                                   "--today", "2026-09-21", "--extractor", "ai",
                                   env={"ANTHROPIC_API_KEY": "test-key-not-real"},
                                   opener=FakeOpener(response))
        self.assertEqual(code, EXIT_OK, stderr)
        row = self.read_csv("import_ready.csv")[0]
        self.assertEqual(row["Currency"], "'=1+1")
        for line in self.read_rows("import_ready.csv"):
            for cell in line:
                self.assertFalse(cell.startswith(("=", "+", "-", "@", "\t", "\r")), repr(cell))

    def test_commas_and_quotes_in_invoice_text_survive_the_round_trip(self):
        self.write_invoice("awkward.txt", AWKWARD)
        self.run_cli()
        rows = self.exceptions()
        self.assertEqual(rows[0]["Check"], "line_math")
        self.assertEqual(rows[0]["Vendor"], 'Quill "and" Vellum, Ltd.')
        self.assertIn('Ream, "letter" size, 20 lb, white', rows[0]["Reason"])
        # Read back by a plain CSV reader, the file has exactly one header and one row, each
        # with the full set of columns: nothing split on the commas inside the cells.
        parsed = list(csv.reader(io.StringIO((self.out / "exceptions.csv").read_text(encoding="utf-8"))))
        self.assertEqual(len(parsed), 2)
        self.assertEqual({len(row) for row in parsed}, {len(EXCEPTION_COLUMNS)})

    def test_a_clean_folder_still_writes_a_sheet_with_only_its_headings(self):
        self.copy_samples("bramblewood_BP-10421.txt", "kestrel_KR-2291.txt")
        _, stdout, _ = self.run_cli()
        self.assertEqual(self.read_rows("exceptions.csv"), [EXCEPTION_COLUMNS])
        self.assertIn("exceptions.csv (0 rows, one per flag)", stdout)


if __name__ == "__main__":
    unittest.main()
