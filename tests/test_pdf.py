"""PDF invoices: read through their text layer with pypdf when it is installed, NOT CHECKED otherwise.

The tests that read a PDF need pypdf and are skipped, with a message, without it.
The tests of what happens without pypdf, of the scan and damaged-file reasons'
plumbing and of the sample writer run either way.
"""

import csv
import importlib.util
import io
import json
import logging
import re
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from invoicecheck import pdftext
from invoicecheck.cli import EXIT_NEEDS_REVIEW, EXIT_OK, EXIT_USAGE, main
from invoicecheck.documents import NotReadable, load_document, load_folder, scan_folder

from .helpers import ROOT, SAMPLES

PDF_SAMPLES = ROOT / "pdf_samples"
PYPDF = pdftext.load_pypdf()
SKIP_WITHOUT_PYPDF = "pypdf is not installed, so the tests that read a PDF are skipped (pip install pypdf to run them)"
needs_pypdf = unittest.skipUnless(PYPDF is not None, SKIP_WITHOUT_PYPDF)


def _load_writer():
    spec = importlib.util.spec_from_file_location("make_pdf_samples", ROOT / "tools" / "make_pdf_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WRITER = _load_writer()


class PdfTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "in"
        self.folder.mkdir()
        self.out = self.root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, data):
        (self.folder / name).write_bytes(data)

    def put_samples(self, *names):
        for name in names:
            source = PDF_SAMPLES / name if name.endswith(".pdf") else SAMPLES / name
            self.put(name, source.read_bytes())

    def run_cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(["run", "--input", str(self.folder), "--out", str(self.out), "--today", "2026-09-21", *extra],
                    env={}, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def results(self):
        return json.loads((self.out / "results.json").read_text(encoding="utf-8"))


class SampleWriterTests(unittest.TestCase):
    """tools/make_pdf_samples.py writes the PDFs itself, with the standard library only."""

    def test_the_samples_are_exactly_what_the_writer_produces(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = WRITER.write_samples(Path(tmp))
            self.assertEqual(sorted(p.name for p in written), sorted(p.name for p in PDF_SAMPLES.iterdir()))
            for path in written:
                self.assertEqual(path.read_bytes(), (PDF_SAMPLES / path.name).read_bytes(),
                                 f"pdf_samples/{path.name} is stale; run python tools/make_pdf_samples.py")

    def test_the_cross_reference_table_points_at_every_object(self):
        # The structure a PDF reader relies on, checked without any PDF library.
        for data in (WRITER.pdf_bytes(WRITER.CLEAN), WRITER.pdf_bytes(scanned=True)):
            self.assertTrue(data.startswith(b"%PDF-1.4\n"))
            self.assertTrue(data.endswith(b"%%EOF\n"))
            startxref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n", 1)[0])
            self.assertTrue(data[startxref:].startswith(b"xref\n0 "))
            header, *entries = data[startxref:].split(b"trailer", 1)[0].splitlines()[1:]
            count = int(header.split()[1])
            self.assertEqual(len(entries), count)
            for number, entry in enumerate(entries[1:], start=1):
                offset = int(entry[:10])
                self.assertTrue(data[offset:].startswith(b"%d 0 obj\n" % number), number)
            self.assertIn(b"/Size %d " % count, data)
            for length, stream in [(int(m), s) for m, s in _streams(data)]:
                self.assertEqual(length, len(stream))

    def test_text_is_placed_cell_by_cell_with_no_spaces_between_columns(self):
        stream = WRITER.text_stream(["Delivery                              1        12.00       12.00"])
        self.assertEqual(stream.count(b" Tj"), 4)
        self.assertIn(b"(Delivery) Tj", stream)
        self.assertIn(b"(12.00) Tj", stream)
        self.assertNotIn(b"  ", stream)

    def test_parentheses_and_backslashes_are_escaped_and_non_ascii_is_refused(self):
        self.assertEqual(WRITER._pdf_string(r"Tax (6%) \ note"), rb"(Tax \(6%\) \\ note)")
        with self.assertRaises(ValueError):
            WRITER._pdf_string("Café")

    def test_a_scan_has_an_image_and_no_text(self):
        data = WRITER.pdf_bytes(scanned=True)
        self.assertIn(b"/Subtype /Image", data)
        self.assertNotIn(b" Tj", data)
        self.assertNotIn(b"/Font", data)

    def test_the_scan_option_writes_a_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "scan_0001.pdf"
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(WRITER.main(["--scan", str(target)]), 0)
            self.assertEqual(target.read_bytes(), WRITER.pdf_bytes(scanned=True))


def _streams(data):
    for m in re.finditer(rb"/Length (\d+)[^>]*>>\nstream\n", data):
        start = m.end()
        end = data.index(b"\nendstream", start)
        yield m.group(1), data[start:end]


class WithoutPypdfTests(PdfTestCase):
    """What happens when pypdf is not installed. These run whether it is installed or not."""

    def test_each_pdf_is_listed_as_not_checked_with_the_install_hint(self):
        self.put_samples("bramblewood_BP-10421.txt", "tumblewick_TW-3301.pdf", "tumblewick_TW-3318.pdf")
        with mock.patch.object(pdftext, "pypdf", None):
            docs, unread = load_folder(self.folder)
        self.assertEqual([d.name for d in docs], ["bramblewood_BP-10421.txt"])
        self.assertEqual([(u.name, u.reason) for u in unread], [
            ("tumblewick_TW-3301.pdf", "install pypdf to read PDF invoices"),
            ("tumblewick_TW-3318.pdf", "install pypdf to read PDF invoices"),
        ])

    def test_the_whole_run_without_pypdf_installed(self):
        # A real interpreter where "import pypdf" fails, so the import fallback itself is exercised.
        self.put_samples("bramblewood_BP-10421.txt", "tumblewick_TW-3301.pdf", "tumblewick_TW-3318.pdf")
        script = ("import sys; sys.modules['pypdf'] = None\n"
                  "from invoicecheck.cli import main\n"
                  "raise SystemExit(main(sys.argv[1:]))\n")
        proc = subprocess.run(
            [sys.executable, "-c", script, "run", "--input", str(self.folder), "--out", str(self.out),
             "--today", "2026-09-21", "--fail-on-review"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, EXIT_NEEDS_REVIEW, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("1 invoice from in/, 2 other files not checked", proc.stdout)
        self.assertIn("  NOT CHECKED   tumblewick_TW-3301.pdf    -                   -  "
                      "install pypdf to read PDF invoices\n", proc.stdout)
        self.assertIn("1 PASS, 0 NEEDS REVIEW, 2 NOT CHECKED", proc.stdout)
        results = self.results()
        self.assertEqual(results["not_checked"], [
            {"file": "tumblewick_TW-3301.pdf", "reason": "install pypdf to read PDF invoices"},
            {"file": "tumblewick_TW-3318.pdf", "reason": "install pypdf to read PDF invoices"},
        ])
        with (self.out / "all_invoices.csv").open(newline="", encoding="utf-8") as handle:
            rows = [r for r in csv.DictReader(handle) if r["Status"] == "NOT CHECKED"]
        self.assertEqual([r["Reasons"] for r in rows], ["install pypdf to read PDF invoices"] * 2)

    def test_a_folder_of_pdfs_only_says_what_to_install(self):
        self.put_samples("tumblewick_TW-3301.pdf")
        with mock.patch.object(pdftext, "pypdf", None):
            code, _, stderr = self.run_cli()
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no invoices it could read", stderr)
        self.assertIn("NOT CHECKED  tumblewick_TW-3301.pdf  (install pypdf to read PDF invoices)", stderr)

    def test_the_extract_command_on_a_pdf_says_what_to_install(self):
        stderr = io.StringIO()
        with mock.patch.object(pdftext, "pypdf", None):
            code = main(["extract", str(PDF_SAMPLES / "tumblewick_TW-3301.pdf")], env={},
                        stdout=io.StringIO(), stderr=stderr)
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(stderr.getvalue(),
                         "NOT CHECKED: tumblewick_TW-3301.pdf: install pypdf to read PDF invoices\n")

    def test_pypdf_is_imported_only_when_a_pdf_turns_up(self):
        # Importing pypdf takes longer than checking the ten samples, so a folder of text
        # and HTML invoices never loads it.
        script = ("import importlib.util, io, sys\n"
                  "from invoicecheck.cli import main\n"
                  "main(sys.argv[1:], env={}, stdout=io.StringIO(), stderr=io.StringIO())\n"
                  "print('pypdf' in sys.modules, importlib.util.find_spec('pypdf') is not None)\n")
        seen = {}
        for name, folder in (("samples", SAMPLES), ("pdf", PDF_SAMPLES)):
            proc = subprocess.run([sys.executable, "-c", script, "run", "--input", str(folder),
                                   "--out", str(self.out / name), "--today", "2026-09-21"],
                                  cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.stderr, "")
            seen[name] = proc.stdout.split()
        loaded, installed = seen["samples"]
        self.assertEqual(loaded, "False")
        loaded, installed = seen["pdf"]
        self.assertEqual(loaded, installed)   # loaded for the PDFs, whenever it is installed

    def test_a_hidden_mac_copy_of_a_pdf_is_skipped_not_reported_as_damaged(self):
        self.put("._invoice.pdf", b"\x00\x05\x16\x07 resource data, not a PDF")
        self.put_samples("bramblewood_BP-10421.txt")
        paths, unread = scan_folder(self.folder)
        self.assertEqual([p.name for p in paths], ["bramblewood_BP-10421.txt"])
        self.assertEqual(unread, [])


class FakePage:
    def __init__(self, layout_fails):
        self.layout_fails = layout_fails
        self.modes = []

    def extract_text(self, extraction_mode="plain"):
        self.modes.append(extraction_mode)
        if extraction_mode == "layout" and self.layout_fails:
            raise ValueError("font the layout mode cannot measure")
        return f"{extraction_mode} text"


class PageTextTests(unittest.TestCase):
    def test_layout_mode_is_used_so_columns_keep_their_spacing(self):
        page = FakePage(layout_fails=False)
        self.assertEqual(pdftext._page_text(page), "layout text")
        self.assertEqual(page.modes, ["layout"])

    def test_plain_mode_is_the_fallback_when_layout_mode_fails(self):
        page = FakePage(layout_fails=True)
        self.assertEqual(pdftext._page_text(page), "plain text")
        self.assertEqual(page.modes, ["layout", "plain"])

    def test_tidy_trims_blank_edges_and_trailing_spaces(self):
        self.assertEqual(pdftext._tidy("\n\n  Vendor   \r\nTotal: 1.00  \n\n"), "  Vendor\nTotal: 1.00\n")
        self.assertEqual(pdftext._tidy(" \n \n"), "")


@needs_pypdf
class PdfReadingTests(PdfTestCase):
    """PDFs with a text layer are read and checked like any other invoice (needs pypdf)."""

    def test_the_clean_pdf_passes_and_the_planted_error_is_caught(self):
        self.put_samples("tumblewick_TW-3301.pdf", "tumblewick_TW-3318.pdf")
        code, stdout, stderr = self.run_cli()
        self.assertEqual((code, stderr), (EXIT_OK, ""))
        verdicts = {i["invoice"]["source_file"]: i for i in self.results()["invoices"]}
        clean, planted = verdicts["tumblewick_TW-3301.pdf"], verdicts["tumblewick_TW-3318.pdf"]
        self.assertEqual((clean["status"], clean["findings"]), ("PASS", []))
        self.assertEqual((planted["status"], [f["check"] for f in planted["findings"]]),
                         ("NEEDS REVIEW", ["line_math"]))
        self.assertEqual(planted["findings"][0]["message"],
                         "Item 2 ('Printed tote bags, pack of 10'): 2 x $36.75 = $73.50, but the line "
                         "shows $75.30 (off by $1.80).")
        self.assertEqual(planted["overbilled"], "1.80")
        self.assertEqual(self.results()["money"]["overbilling_found"], "1.80")
        self.assertIn("1 PASS, 1 NEEDS REVIEW, 0 NOT CHECKED", stdout)

        with (self.out / "import_ready.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({r["InvoiceNumber"] for r in rows}, {"TW-3301"})
        self.assertEqual(sum((Decimal(r["LineAmount"]) for r in rows), Decimal("0")), Decimal("450.00"))

    def test_the_text_layer_is_read_field_by_field(self):
        doc = load_document(PDF_SAMPLES / "tumblewick_TW-3301.pdf")
        self.assertEqual(doc.kind, "text")
        self.assertIn("\nDescription                          Qty   Unit Price      Amount\n", doc.text)
        stdout = io.StringIO()
        code = main(["extract", str(PDF_SAMPLES / "tumblewick_TW-3301.pdf")], env={}, stdout=stdout,
                    stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        data = json.loads(stdout.getvalue())
        self.assertEqual(
            (data["layout"], data["vendor"], data["invoice_number"], data["invoice_date"], data["due_date"]),
            ("text-columns", "Tumblewick Print Works", "TW-3301", "2026-08-18", "2026-09-17"))
        self.assertEqual([(i["quantity"], i["unit_price"], i["amount"]) for i in data["line_items"]],
                         [("4", "38.00", "152.00"), ("10", "12.50", "125.00"),
                          ("2", "64.00", "128.00"), ("1", "45.00", "45.00")])
        self.assertEqual((data["subtotal"], data["tax"], data["tax_rate_percent"], data["total"], data["warnings"]),
                         ("450.00", "27.00", "6", "477.00", []))

    def test_a_scanned_pdf_is_not_checked_because_there_is_no_ocr(self):
        self.put("scan_0001.pdf", WRITER.pdf_bytes(scanned=True))
        self.put_samples("bramblewood_BP-10421.txt")
        code, stdout, _ = self.run_cli("--fail-on-review")
        self.assertEqual(code, EXIT_NEEDS_REVIEW)
        reason = "scanned PDF (no text layer), not read: it needs OCR first, which this tool does not do"
        self.assertEqual(self.results()["not_checked"], [{"file": "scan_0001.pdf", "reason": reason}])
        self.assertIn(f"  NOT CHECKED   scan_0001.pdf             -                   -  {reason}\n", stdout)
        self.assertIn(f"| scan_0001.pdf | {reason} |", (self.out / "review_report.md").read_text(encoding="utf-8"))

    def test_a_damaged_pdf_is_not_checked_and_makes_no_log_noise(self):
        logger = logging.getLogger("pypdf")
        before = logger.level
        # Bytes that make pypdf log warnings ("invalid pdf header", "EOF marker not found")
        # before it gives up, so the test fails if those reach the log.
        with self.assertNoLogs("pypdf", level="WARNING"):
            with self.assertRaises(NotReadable) as caught:
                pdftext.pdf_text(b"not a pdf at all")
        self.assertRegex(caught.exception.reason,
                         r"^PDF, not read: it could not be opened \(\w+\); the file may be damaged$")
        self.assertEqual(logger.level, before)

    def test_a_damaged_pdf_among_good_ones_is_listed_and_the_rest_are_checked(self):
        self.put("broken.pdf", b"not a pdf at all")
        self.put_samples("tumblewick_TW-3301.pdf")
        code, stdout, stderr = self.run_cli()
        self.assertEqual((code, stderr), (EXIT_OK, ""))
        self.assertIn("1 PASS, 0 NEEDS REVIEW, 1 NOT CHECKED", stdout)
        [item] = self.results()["not_checked"]
        self.assertEqual(item["file"], "broken.pdf")
        self.assertTrue(item["reason"].startswith("PDF, not read: it could not be opened ("), item["reason"])

    def test_a_password_protected_pdf_is_not_checked(self):
        locked = mock.Mock(side_effect=PYPDF.errors.FileNotDecryptedError("File has not been decrypted"))
        with mock.patch.object(PYPDF, "PdfReader", locked):
            with self.assertRaises(NotReadable) as caught:
                pdftext.pdf_text(b"%PDF-1.7 ...")
        self.assertEqual(caught.exception.reason, "PDF, not read: it is password-protected")

    def test_an_encrypted_pdf_that_needs_another_package_says_which(self):
        # pypdf's own message when an AES-encrypted PDF meets a machine without cryptography.
        missing = mock.Mock(side_effect=PYPDF.errors.DependencyError(
            "cryptography>=3.1 is required for AES algorithm"))
        with mock.patch.object(PYPDF, "PdfReader", missing):
            with self.assertRaises(NotReadable) as caught:
                pdftext.pdf_text(b"%PDF-1.7 ...")
        self.assertEqual(caught.exception.reason, "PDF, not read: pypdf needs another package to open it "
                                                  "(cryptography>=3.1 is required for AES algorithm)")

    def test_a_pdf_with_no_pages_is_not_checked(self):
        empty = mock.Mock(return_value=mock.Mock(pages=[]))
        with mock.patch.object(PYPDF, "PdfReader", empty):
            with self.assertRaises(NotReadable) as caught:
                pdftext.pdf_text(b"%PDF-1.7 ...")
        self.assertEqual(caught.exception.reason, "PDF, not read: it has no pages")

    def test_a_pdf_whose_text_is_not_an_invoice_is_held_not_passed(self):
        self.put("letter.pdf", WRITER.pdf_bytes(["Dear customer,", "", "Our office moves on 1 October."]))
        self.run_cli()
        [item] = self.results()["invoices"]
        self.assertEqual(item["status"], "NEEDS REVIEW")
        self.assertIn("extraction", [f["check"] for f in item["findings"]])

    def test_a_pdf_sent_twice_is_caught_like_any_other_duplicate(self):
        self.put_samples("tumblewick_TW-3301.pdf")
        self.put("tumblewick_TW-3301_copy.pdf", (PDF_SAMPLES / "tumblewick_TW-3301.pdf").read_bytes())
        self.run_cli()
        money = self.results()["money"]
        self.assertEqual((money["overbilling_found"], money["sent_twice_copies"], money["sent_twice_total"]),
                         ("0.00", 1, "477.00"))


if __name__ == "__main__":
    unittest.main()
