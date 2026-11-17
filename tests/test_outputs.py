"""The writers: import CSV, summary CSV, Markdown and HTML reports, JSON."""

import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from invoicecheck.documents import Unread
from invoicecheck.model import LineItem
from invoicecheck.textutil import cents
from invoicecheck.outputs import (
    IMPORT_COLUMNS,
    SUMMARY_COLUMNS,
    safe_text,
    write_import_csv,
    write_results_json,
    write_review_html,
    write_review_report,
    write_summary_csv,
)
from invoicecheck.verify import verify_all

from .helpers import TODAY, D, make_invoice


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        good = make_invoice(source_file="good.txt", invoice_number="G-1")
        bad = make_invoice(source_file="bad.txt", invoice_number="B-1", total=D("99.00"))
        self.verdicts = verify_all([good, bad], TODAY)

    def tearDown(self):
        self.tmp.cleanup()

    def read_csv(self, name):
        with (self.out / name).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_import_csv_holds_only_passing_invoices(self):
        built = write_import_csv(self.verdicts, self.out / "import.csv")
        data = self.read_csv("import.csv")
        self.assertEqual((len(built.rows), built.invoices, built.passed, built.approved), (2, 1, 1, 0))
        self.assertEqual(list(data[0]), IMPORT_COLUMNS)
        self.assertEqual({r["InvoiceNumber"] for r in data}, {"G-1"})
        self.assertEqual(data[0]["UnitPrice"], "10.00")
        self.assertEqual(data[0]["InvoiceTotal"], "39.42")

    def test_summary_csv_lists_every_invoice_with_reasons(self):
        write_summary_csv(self.verdicts, self.out / "all.csv")
        data = self.read_csv("all.csv")
        self.assertEqual([(r["SourceFile"], r["Status"]) for r in data],
                         [("good.txt", "PASS"), ("bad.txt", "NEEDS REVIEW")])
        self.assertIn("[total_math]", data[1]["Reasons"])

    def test_review_report_names_the_file_and_the_reason(self):
        write_review_report(self.verdicts, self.out / "r.md", today=TODAY, input_label="in", extractor="rules")
        text = (self.out / "r.md").read_text(encoding="utf-8")
        self.assertIn("## Needs a human (1)", text)
        self.assertIn("### bad.txt", text)
        self.assertIn("- **Total is wrong.** Subtotal $36.50 + tax $2.92 = $39.42, but the total says $99.00 "
                      "(off by $59.58). `total_math`", text)
        self.assertIn("| good.txt |", text)

    def test_results_json(self):
        write_results_json(self.verdicts, self.out / "r.json", today=TODAY, input_label="in", extractor="rules")
        data = json.loads((self.out / "r.json").read_text(encoding="utf-8"))
        self.assertEqual(data["counts"],
                         {"PASS": 1, "APPROVED": 0, "NEEDS REVIEW": 1, "NOT CHECKED": 0})
        self.assertEqual(data["invoices"][1]["findings"][0]["check"], "total_math")
        self.assertEqual(data["invoices"][0]["invoice"]["total"], "39.42")

    def test_unit_prices_are_written_as_printed_not_rounded(self):
        items = [LineItem("Cloud backup, per GB", D("320"), D("0.125"), D("40.00")),
                 LineItem("API calls", D("2500"), D("0.004"), D("10.00")),
                 LineItem("Setup", D("1"), D("42.5"), D("42.50"))]
        inv = make_invoice(line_items=items, subtotal=D("92.50"), tax=None, tax_rate=None, total=D("92.50"))
        verdicts = verify_all([inv], TODAY)
        self.assertEqual(verdicts[0].status, "PASS")

        write_import_csv(verdicts, self.out / "i.csv")
        rows = self.read_csv("i.csv")
        self.assertEqual([(r["Quantity"], r["UnitPrice"], r["LineAmount"]) for r in rows],
                         [("320", "0.125", "40.00"), ("2500", "0.004", "10.00"), ("1", "42.50", "42.50")])
        # An importer that recomputes each line from quantity x unit price gets the printed amount.
        for r in rows:
            self.assertEqual(cents(Decimal(r["Quantity"]) * Decimal(r["UnitPrice"])), Decimal(r["LineAmount"]))

        write_results_json(verdicts, self.out / "r.json", today=TODAY, input_label="in", extractor="rules")
        data = json.loads((self.out / "r.json").read_text(encoding="utf-8"))
        self.assertEqual([i["unit_price"] for i in data["invoices"][0]["invoice"]["line_items"]],
                         ["0.125", "0.004", "42.50"])

    def test_files_not_checked_appear_in_every_report(self):
        unread = [Unread("scan_0001.pdf", "scanned PDF (no text layer), not read: it needs OCR first, which this tool does not do")]
        report = {"today": TODAY, "input_label": "in", "extractor": "rules", "unread": unread}
        write_summary_csv(self.verdicts, self.out / "all.csv", unread=unread)
        write_review_report(self.verdicts, self.out / "r.md", **report)
        write_review_html(self.verdicts, self.out / "r.html", **report)
        write_results_json(self.verdicts, self.out / "r.json", **report)

        rows = self.read_csv("all.csv")
        self.assertEqual(list(rows[0]), SUMMARY_COLUMNS)
        self.assertEqual((rows[2]["SourceFile"], rows[2]["Status"], rows[2]["Reasons"]),
                         ("scan_0001.pdf", "NOT CHECKED", "scanned PDF (no text layer), not read: it needs OCR first, which this tool does not do"))
        md = (self.out / "r.md").read_text(encoding="utf-8")
        self.assertIn("- Result: 1 PASS, 1 NEEDS REVIEW, 1 NOT CHECKED", md)
        self.assertIn("## Not checked (1)", md)
        self.assertIn("| scan_0001.pdf | scanned PDF (no text layer), not read: it needs OCR first, which this tool does not do |", md)
        page = (self.out / "r.html").read_text(encoding="utf-8")
        self.assertIn("<h2>Not checked (1)</h2>", page)
        self.assertIn("<td>scan_0001.pdf</td>", page)
        data = json.loads((self.out / "r.json").read_text(encoding="utf-8"))
        self.assertEqual(data["counts"],
                         {"PASS": 1, "APPROVED": 0, "NEEDS REVIEW": 1, "NOT CHECKED": 1})
        self.assertEqual(data["not_checked"],
                         [{"file": "scan_0001.pdf", "reason": "scanned PDF (no text layer), not read: it needs OCR first, which this tool does not do"}])

    def test_overbilling_is_in_the_csv_the_reports_and_the_json(self):
        report = {"today": TODAY, "input_label": "in", "extractor": "rules"}
        write_summary_csv(self.verdicts, self.out / "all.csv")
        write_review_report(self.verdicts, self.out / "r.md", **report)
        write_review_html(self.verdicts, self.out / "r.html", **report)
        write_results_json(self.verdicts, self.out / "r.json", **report)
        self.assertEqual([(r["Overbilled"], r["SentTwiceAmount"]) for r in self.read_csv("all.csv")],
                         [("0.00", "0.00"), ("59.58", "0.00")])
        md = (self.out / "r.md").read_text(encoding="utf-8")
        self.assertIn("- Overbilling found: $59.58 in arithmetic errors\n- Only PASS", md)
        self.assertIn("Total: $99.00 - Overbilled by: $59.58\n", md)
        self.assertNotIn("Sent twice", md)
        page = (self.out / "r.html").read_text(encoding="utf-8")
        self.assertIn("Overbilled by $59.58", page)
        self.assertIn('<div class="tile "><div class="n">$0.00</div><div class="l">no invoice sent twice</div>', page)
        data = json.loads((self.out / "r.json").read_text(encoding="utf-8"))
        self.assertEqual(data["money"], {"held_for_review_total": "99.00", "overbilling_found": "59.58",
                                         "sent_twice_copies": 0, "sent_twice_total": "0.00",
                                         "approved_invoices": 0, "approved_total": "0.00",
                                         "approved_overbilling": "0.00"})
        self.assertEqual(data["invoices"][1]["findings"][0]["overcharge"], "59.58")
        self.assertIsNone(data["invoices"][1]["findings"][0]["sent_twice_amount"])

    def test_several_invoices_sent_twice_are_counted_and_totalled_apart_from_overbilling(self):
        verdicts = verify_all([
            make_invoice(source_file="a1.txt", invoice_number="A-1", invoice_date=TODAY.replace(day=1)),
            make_invoice(source_file="a2.txt", invoice_number="A-1", invoice_date=TODAY.replace(day=1)),
            make_invoice(source_file="b1.txt", invoice_number="B-1", invoice_date=TODAY.replace(day=2),
                         total=D("49.42")),
            make_invoice(source_file="b2.txt", invoice_number="B-1", invoice_date=TODAY.replace(day=2),
                         total=D("49.42")),
        ], TODAY)
        report = {"today": TODAY, "input_label": "in", "extractor": "rules"}
        write_review_report(verdicts, self.out / "r.md", **report)
        write_review_html(verdicts, self.out / "r.html", **report)
        md = (self.out / "r.md").read_text(encoding="utf-8")
        # b1 overbills by $10.00 (its total is $10 more than subtotal + tax); b2 is the same invoice again.
        self.assertIn("- Overbilling found: $10.00 in arithmetic errors\n"
                      "- Sent twice: 2 invoices totalling $88.84, held so none is paid twice\n", md)
        self.assertIn("2 invoices sent twice, held so none is paid twice", (self.out / "r.html").read_text(encoding="utf-8"))

    def test_html_report_escapes_invoice_text_and_loads_nothing(self):
        hostile = make_invoice(source_file="<b>x</b>.html", vendor='<script>alert("x")</script> & Co',
                               total=D("99.00"))
        write_review_html(verify_all([hostile], TODAY), self.out / "r.html", today=TODAY,
                          input_label="in", extractor="rules")
        page = (self.out / "r.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", page)
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; Co", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;.html", page)
        # One self-contained file: no scripts, stylesheets, images or fonts fetched from anywhere.
        for fetch in ("src=", "href=", "@import", "url("):
            self.assertNotIn(fetch, page)

    def test_formula_like_text_is_neutralised(self):
        self.assertEqual(safe_text("=HYPERLINK(\"x\")"), "'=HYPERLINK(\"x\")")
        self.assertEqual(safe_text("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(safe_text("Copy paper"), "Copy paper")
        inv = make_invoice(line_items=[LineItem("=1+1", D("1"), D("36.50"), D("36.50"))])
        write_import_csv(verify_all([inv], TODAY), self.out / "i.csv")
        self.assertEqual(self.read_csv("i.csv")[0]["Description"], "'=1+1")


if __name__ == "__main__":
    unittest.main()
