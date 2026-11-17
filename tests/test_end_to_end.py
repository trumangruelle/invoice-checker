"""The whole pipeline on samples/, through the same main() the command line uses."""

import contextlib
import csv
import io
import json
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from invoicecheck import pdftext
from invoicecheck.cli import EXIT_NEEDS_REVIEW, EXIT_OK, EXIT_USAGE, main

from .helpers import SAMPLES

# What each sample is supposed to produce. The planted errors are listed in docs/samples.md.
EXPECTED = {
    "bramblewood_BP-10421.txt": ("PASS", []),
    "bramblewood_BP-10437.txt": ("NEEDS REVIEW", ["lines_sum"]),
    "kestrel_KR-2291.txt": ("PASS", []),
    "kestrel_KR-2304.txt": ("NEEDS REVIEW", ["line_math"]),
    "kestrel_KR-2310.txt": ("NEEDS REVIEW", ["date_future", "due_before_issue"]),
    "lanternfish_LF-0087.html": ("PASS", []),
    "lanternfish_LF-0091.html": ("NEEDS REVIEW", ["tax_rate"]),
    "mossgate_MG-5498.html": ("NEEDS REVIEW", ["total_math"]),
    "mossgate_MG-5512.html": ("NEEDS REVIEW", ["duplicate_number"]),
    "mossgate_MG-5512_reminder.html": ("NEEDS REVIEW", ["duplicate_number"]),
}

# Overbilling per held invoice: the "off by" amounts in the vendor's favour. LF-0091
# undercharges tax, so it counts nothing. MG-5512 is sent twice, which is not overbilling.
EXPECTED_OVERBILLED = {
    "bramblewood_BP-10437.txt": "9.00",
    "kestrel_KR-2304.txt": "15.00",
    "kestrel_KR-2310.txt": "0.00",
    "lanternfish_LF-0091.html": "0.00",
    "mossgate_MG-5498.html": "10.00",
    "mossgate_MG-5512.html": "0.00",
    "mossgate_MG-5512_reminder.html": "0.00",
}
# The second copy of MG-5512 carries its whole total: what paying it too would pay twice.
EXPECTED_SENT_TWICE = {name: "0.00" for name in EXPECTED_OVERBILLED} | {"mossgate_MG-5512_reminder.html": "1197.00"}

OUTPUT_FILES = ("import_ready.csv", "all_invoices.csv", "exceptions.csv", "review_report.html",
                "review_report.md", "results.json")
EXAMPLE_OUTPUT = SAMPLES.parent / "example_output"


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra, env=None, folder=SAMPLES):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(["run", "--input", str(folder), "--out", str(self.out), "--today", "2026-09-21", *extra],
                    env=env if env is not None else {}, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def batch(self, *names):
        """A scratch input folder holding the named samples."""
        folder = Path(self.tmp.name) / "batch"
        folder.mkdir()
        for name in names:
            shutil.copy(SAMPLES / name, folder / name)
        return folder

    def results(self):
        return json.loads((self.out / "results.json").read_text(encoding="utf-8"))

    def test_overbilling_found_on_the_samples(self):
        _, stdout, _ = self.run_cli()
        results = self.results()
        held = [i for i in results["invoices"] if i["status"] == "NEEDS REVIEW"]
        self.assertEqual({i["invoice"]["source_file"]: i["overbilled"] for i in held}, EXPECTED_OVERBILLED)
        self.assertEqual({i["invoice"]["source_file"]: i["sent_twice_amount"] for i in held}, EXPECTED_SENT_TWICE)
        self.assertEqual(results["money"], {
            "held_for_review_total": "6643.95",
            "overbilling_found": "34.00",
            "sent_twice_copies": 1,
            "sent_twice_total": "1197.00",
            "approved_invoices": 0,
            "approved_total": "0.00",
            "approved_overbilling": "0.00",
        })
        self.assertIn("Held for review: 7 invoices totalling $6,643.95. "
                      "Overbilling found: $34.00 in arithmetic errors.\n"
                      "Sent twice: a $1,197.00 invoice, held so it is not paid twice.\n", stdout)

    def test_the_invoice_sent_twice_is_reported_apart_from_overbilling_in_every_output(self):
        self.run_cli()
        md = (self.out / "review_report.md").read_text(encoding="utf-8")
        self.assertIn("- Overbilling found: $34.00 in arithmetic errors\n"
                      "- Sent twice: a $1,197.00 invoice, held so it is not paid twice\n", md)
        self.assertIn("Total: $1,197.00 - Second copy of a $1,197.00 invoice: paying this copy too would pay "
                      "it twice", md)
        page = (self.out / "review_report.html").read_text(encoding="utf-8")
        self.assertIn('<div class="n">$34.00</div><div class="l">overbilling found (arithmetic errors)</div>', page)
        self.assertIn('<div class="tile twice"><div class="n">$1,197.00</div>'
                      '<div class="l">1 invoice sent twice, held so it is not paid twice</div>', page)
        self.assertIn('<span class="over twice">Second copy of a $1,197.00 invoice</span>', page)
        self.assertNotIn("Overbilled by $1,197.00", page)
        with (self.out / "all_invoices.csv").open(newline="", encoding="utf-8") as handle:
            rows = {r["SourceFile"]: r for r in csv.DictReader(handle)}
        reminder = rows["mossgate_MG-5512_reminder.html"]
        self.assertEqual((reminder["Overbilled"], reminder["SentTwiceAmount"]), ("0.00", "1197.00"))
        self.assertEqual((rows["mossgate_MG-5512.html"]["Overbilled"], rows["mossgate_MG-5512.html"]["SentTwiceAmount"]),
                         ("0.00", "0.00"))

    def test_console_summary_says_each_reason_in_plain_words(self):
        _, stdout, _ = self.run_cli()
        lines = stdout.splitlines()
        self.assertTrue(any(line.startswith("  NEEDS REVIEW  kestrel_KR-2310.txt")
                            and line.endswith("date in the future, due before it was issued") for line in lines))
        self.assertTrue(any("bramblewood_BP-10437.txt" in line and line.endswith("items don't add up")
                            for line in lines))

    def test_files_it_cannot_read_are_listed_not_dropped(self):
        batch = Path(self.tmp.name) / "batch"
        shutil.copytree(SAMPLES, batch)
        (batch / "supplier_invoice.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (batch / "receipt_photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (batch / "Thumbs.db").write_bytes(b"")
        # As on a machine without pypdf; the PDF reasons with pypdf are in tests/test_pdf.py.
        with mock.patch.object(pdftext, "pypdf", None):
            code, stdout, _ = self.run_cli("--fail-on-review", folder=batch)

        self.assertEqual(code, EXIT_NEEDS_REVIEW)
        self.assertIn("10 invoices from batch/, 2 other files not checked", stdout)
        self.assertIn("\n  NOT CHECKED   receipt_photo.jpg ", stdout)
        self.assertIn("  install pypdf to read PDF invoices\n", stdout)
        self.assertIn("3 PASS, 7 NEEDS REVIEW, 2 NOT CHECKED", stdout)
        self.assertNotIn("Thumbs.db", stdout)

        report = (self.out / "review_report.md").read_text(encoding="utf-8")
        self.assertIn("## Not checked (2)", report)
        self.assertIn("| receipt_photo.jpg | image, not read: it needs OCR first |", report)
        self.assertIn("| supplier_invoice.pdf | install pypdf to read PDF invoices |", report)
        with (self.out / "all_invoices.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r["SourceFile"] for r in rows if r["Status"] == "NOT CHECKED"],
                         ["receipt_photo.jpg", "supplier_invoice.pdf"])
        results = self.results()
        self.assertEqual(results["counts"],
                         {"PASS": 3, "APPROVED": 0, "NEEDS REVIEW": 7, "NOT CHECKED": 2})
        self.assertEqual([u["file"] for u in results["not_checked"]], ["receipt_photo.jpg", "supplier_invoice.pdf"])
        self.assertIn("<h2>Not checked (2)</h2>", (self.out / "review_report.html").read_text(encoding="utf-8"))

    def test_fail_on_review_counts_a_file_that_was_not_checked(self):
        folder = self.batch("bramblewood_BP-10421.txt")
        (folder / "scan_0001.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        code, stdout, _ = self.run_cli(folder=folder)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 PASS, 0 NEEDS REVIEW, 1 NOT CHECKED", stdout)
        self.assertIn("import_ready.csv (4 line rows from 1 PASS invoice)", stdout)
        code, _, _ = self.run_cli("--fail-on-review", folder=folder)
        self.assertEqual(code, EXIT_NEEDS_REVIEW)

    def test_a_folder_with_nothing_readable_says_what_it_found(self):
        folder = self.batch()
        (folder / "scan_0001.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (folder / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        with mock.patch.object(pdftext, "pypdf", None):
            code, _, stderr = self.run_cli(folder=folder)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no invoices it could read", stderr)
        self.assertIn("NOT CHECKED  photo.jpg  (image, not read: it needs OCR first)", stderr)
        self.assertIn("NOT CHECKED  scan_0001.pdf  (install pypdf to read PDF invoices)", stderr)
        self.assertFalse(self.out.exists())

    def test_a_file_that_is_not_utf8_is_held_for_review(self):
        folder = self.batch("bramblewood_BP-10421.txt", "kestrel_KR-2291.txt")
        path = folder / "bramblewood_BP-10421.txt"
        data = path.read_bytes()
        self.assertEqual(data.count(b"Recycled"), 1)
        path.write_bytes(data.replace(b"Recycled", b"R\xe9cycled"))   # a Windows-1252 accent
        self.run_cli(folder=folder)
        verdicts = {i["invoice"]["source_file"]: i for i in self.results()["invoices"]}
        flagged = verdicts["bramblewood_BP-10421.txt"]
        self.assertEqual((flagged["status"], [f["check"] for f in flagged["findings"]]),
                         ("NEEDS REVIEW", ["extraction"]))
        self.assertIn("not valid UTF-8 text; 1 unreadable character(s) were replaced", flagged["findings"][0]["message"])
        self.assertEqual(verdicts["kestrel_KR-2291.txt"]["status"], "PASS")

    def test_example_output_matches_a_fresh_run(self):
        # example_output/ is what the README links to; it must be what the command produces today.
        self.run_cli()
        self.assertEqual(sorted(p.name for p in EXAMPLE_OUTPUT.iterdir()), sorted(OUTPUT_FILES))
        for name in OUTPUT_FILES:
            fresh = (self.out / name).read_text(encoding="utf-8").replace("\r\n", "\n")
            shipped = (EXAMPLE_OUTPUT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
            self.assertEqual(shipped, fresh, f"example_output/{name} is stale; regenerate it (see README)")

    def test_every_sample_gets_its_expected_verdict_and_reasons(self):
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, EXIT_OK)
        results = json.loads((self.out / "results.json").read_text(encoding="utf-8"))
        actual = {
            item["invoice"]["source_file"]: (item["status"], [f["check"] for f in item["findings"]])
            for item in results["invoices"]
        }
        self.assertEqual(actual, EXPECTED)
        self.assertIn("3 PASS, 7 NEEDS REVIEW", stdout)

    def test_import_csv_contains_exactly_the_passing_invoices(self):
        self.run_cli()
        with (self.out / "import_ready.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(sorted({r["InvoiceNumber"] for r in rows}), ["BP-10421", "KR-2291", "LF-0087"])
        self.assertEqual(len(rows), 12)
        by_invoice = {}
        for r in rows:
            by_invoice.setdefault(r["InvoiceNumber"], []).append(r)
        # The line amounts in the CSV add up exactly to each invoice's subtotal.
        for number, lines in by_invoice.items():
            total = sum((Decimal(r["LineAmount"]) for r in lines), Decimal("0"))
            self.assertEqual(total, Decimal(lines[0]["InvoiceSubtotal"]), number)

    def test_review_report_lists_each_flagged_file_with_its_reason(self):
        self.run_cli()
        report = (self.out / "review_report.md").read_text(encoding="utf-8")
        for name, (status, checks) in EXPECTED.items():
            if status == "NEEDS REVIEW":
                self.assertIn(f"### {name}", report)
                for check in checks:
                    self.assertIn(f"`{check}`", report)
        self.assertIn("Line items add up to $1,012.40 but the subtotal says $1,021.40", report)
        self.assertIn("12 x $35.00 = $420.00, but the line shows $435.00", report)
        self.assertIn("8.875% of the $681.00 subtotal is $60.44", report)

    def test_outputs_do_not_leak_absolute_paths(self):
        _, stdout, _ = self.run_cli()   # --input is given as an absolute path
        parent = str(SAMPLES.parent)
        texts = {name: (self.out / name).read_text(encoding="utf-8")
                 for name in OUTPUT_FILES}
        texts["stdout"] = stdout
        for name, text in texts.items():
            self.assertNotIn(parent, text, name)
            self.assertNotIn(parent.replace("\\", "/"), text, name)

    def test_printed_paths_are_relative_to_the_working_directory(self):
        previous = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            _, stdout, _ = self.run_cli()   # --out is an absolute path inside the working directory
        finally:
            os.chdir(previous)
        self.assertIn("Wrote out/import_ready.csv (12 line rows from 3 PASS invoices)", stdout)
        self.assertIn("Wrote out/results.json", stdout)
        self.assertNotIn(Path(self.tmp.name).resolve().as_posix(), stdout)
        self.assertNotIn(str(Path(self.tmp.name).resolve()), stdout)

    def test_an_output_folder_outside_the_working_directory_is_printed_relative_too(self):
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        self.out = Path(self.tmp.name) / "elsewhere" / "out"
        previous = os.getcwd()
        os.chdir(work)
        try:
            _, stdout, _ = self.run_cli()   # --out is an absolute path beside the working directory
        finally:
            os.chdir(previous)
        self.assertIn("Wrote ../elsewhere/out/import_ready.csv (12 line rows from 3 PASS invoices)", stdout)
        self.assertIn("Wrote ../elsewhere/out/review_report.html", stdout)
        self.assertNotIn(Path(self.tmp.name).resolve().as_posix(), stdout)
        self.assertNotIn(str(Path(self.tmp.name).resolve()), stdout)

    def test_resent_copies_with_a_banner_on_top_are_still_duplicates(self):
        batch = Path(self.tmp.name) / "batch"
        shutil.copytree(SAMPLES, batch)
        resent = {
            "bramblewood_BP-10421.txt": ("Bramblewood Paper Co.\n",
                                         "PAYMENT REMINDER - SECOND NOTICE\nBramblewood Paper Co.\n"),
            "kestrel_KR-2291.txt": ("Kestrelrook IT Services\n", "COPY\nKestrelrook IT Services\n"),
            "lanternfish_LF-0087.html": ('<h1 class="vendor-name">Lanternfish Catering LLC</h1>',
                                         "<h1>Payment reminder</h1><h2>Lanternfish Catering LLC</h2>"),
        }
        for name, (old, new) in resent.items():
            text = (SAMPLES / name).read_text(encoding="utf-8")
            self.assertEqual(text.count(old), 1, name)
            stem, suffix = name.rsplit(".", 1)
            (batch / f"{stem}_resent.{suffix}").write_text(text.replace(old, new), encoding="utf-8")

        stdout = io.StringIO()
        main(["run", "--input", str(batch), "--out", str(self.out), "--today", "2026-09-21"],
             env={}, stdout=stdout, stderr=io.StringIO())
        results = json.loads((self.out / "results.json").read_text(encoding="utf-8"))
        checks = {i["invoice"]["source_file"]: {f["check"] for f in i["findings"]} for i in results["invoices"]}
        vendors = {i["invoice"]["source_file"]: i["invoice"]["vendor"] for i in results["invoices"]}
        for name in resent:
            stem, suffix = name.rsplit(".", 1)
            copy = f"{stem}_resent.{suffix}"
            self.assertEqual(checks[name], {"duplicate_number"}, name)
            self.assertEqual(checks[copy], {"duplicate_number"}, copy)
            self.assertEqual(vendors[copy], vendors[name], copy)
        with (self.out / "import_ready.csv").open(newline="", encoding="utf-8") as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])
        self.assertIn("0 PASS, 13 NEEDS REVIEW", stdout.getvalue())

    def test_ai_run_survives_a_timeout_on_every_call(self):
        def timing_out(request, timeout=None):
            raise TimeoutError("The read operation timed out")

        stdout = io.StringIO()
        code = main(["run", "--input", str(SAMPLES), "--out", str(self.out), "--today", "2026-09-21",
                     "--extractor", "ai"], env={"ANTHROPIC_API_KEY": "test-key-not-real"},
                    opener=timing_out, sleep=lambda _s: None, stdout=stdout, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        results = json.loads((self.out / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(results["counts"],
                         {"PASS": 0, "APPROVED": 0, "NEEDS REVIEW": 10, "NOT CHECKED": 0})
        for item in results["invoices"]:
            self.assertIn("AI extraction failed: could not reach the Messages API (TimeoutError",
                          item["findings"][0]["message"])
        self.assertIn("0 PASS, 10 NEEDS REVIEW", stdout.getvalue())
        self.assertNotIn("test-key-not-real", stdout.getvalue())
        for name in OUTPUT_FILES:
            self.assertNotIn("test-key-not-real", (self.out / name).read_text(encoding="utf-8"), name)

    def test_fail_on_review_sets_the_exit_code(self):
        code, _, _ = self.run_cli("--fail-on-review")
        self.assertEqual(code, EXIT_NEEDS_REVIEW)

    def test_history_flags_invoices_from_an_earlier_run(self):
        self.run_cli()
        history = Path(self.tmp.name) / "last_month.csv"
        shutil.copy(self.out / "import_ready.csv", history)
        self.run_cli("--history", str(history))
        results = json.loads((self.out / "results.json").read_text(encoding="utf-8"))
        seen = sorted(i["invoice"]["invoice_number"] for i in results["invoices"]
                      if any(f["check"] == "already_seen" for f in i["findings"]))
        self.assertEqual(seen, ["BP-10421", "KR-2291", "LF-0087"])

    def test_ai_mode_without_a_key_stops_with_a_clear_message(self):
        code, _, stderr = self.run_cli("--extractor", "ai", env={})
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("ANTHROPIC_API_KEY", stderr)
        self.assertFalse(self.out.exists())

    def test_missing_input_folder(self):
        stderr = io.StringIO()
        code = main(["run", "--input", str(Path(self.tmp.name) / "nope"), "--out", str(self.out)],
                    env={}, stdout=io.StringIO(), stderr=stderr)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("input folder not found", stderr.getvalue())

    # ------------------------------------------------ --out has to be a folder

    def test_an_out_path_that_is_a_file_says_so_instead_of_crashing(self):
        # mkdir(exist_ok=True) only forgives a folder that is already there. A file
        # sitting on that path used to reach the user as a FileExistsError traceback.
        # Checked before anything is read, so an --extractor ai run does not pay for
        # the API calls first.
        blocked = Path(self.tmp.name) / "out.csv"
        blocked.write_text("not a folder\n", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(["run", "--input", str(SAMPLES), "--out", str(blocked),
                     "--today", "2026-09-21"], env={}, stdout=stdout, stderr=stderr)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn(f"error: --out {blocked} is a file, not a folder; give a folder name, "
                      f"or move that file out of the way", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(blocked.read_text(encoding="utf-8"), "not a folder\n")

    def test_an_out_folder_that_cannot_be_created_says_so_instead_of_crashing(self):
        # A file part-way up the path. exists() is False for this one, so the check
        # above cannot see it and mkdir is what fails; both ways out have to be a
        # message, not a stack trace.
        blocked = Path(self.tmp.name) / "out.csv"
        blocked.write_text("not a folder\n", encoding="utf-8")
        target = blocked / "reports"
        self.assertFalse(target.exists())
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(["run", "--input", str(SAMPLES), "--out", str(target),
                     "--today", "2026-09-21"], env={}, stdout=stdout, stderr=stderr)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn(f"error: could not create the output folder {target}", stderr.getvalue())
        self.assertIn("nothing was written", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    # ------------------------------------------- --today means the same on 3.10

    def test_today_takes_only_yyyy_mm_dd_so_the_floor_and_newer_agree(self):
        # date.fromisoformat() reads "20260923" and week dates from Python 3.11, and
        # only "YYYY-MM-DD" on the 3.10 floor the client deploys on. The CLI pins the
        # format itself so one command does not mean two things.
        for bad in ("20260923", "2026-W39-3", "2026-09-23T00:00:00", "2026-9-23", "today"):
            with self.subTest(today=bad):
                argv_stderr = io.StringIO()
                with contextlib.redirect_stderr(argv_stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main(["run", "--input", str(SAMPLES), "--out", str(self.out),
                              "--today", bad], env={}, stdout=io.StringIO(), stderr=io.StringIO())
                self.assertEqual(raised.exception.code, EXIT_USAGE)
                self.assertIn(f"expected YYYY-MM-DD, got {bad!r}", argv_stderr.getvalue())
                self.assertFalse(self.out.exists())

    def test_the_documented_today_spelling_is_the_one_that_works(self):
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("dates checked against 2026-09-21", stdout)

    def test_extract_command_prints_json(self):
        stdout = io.StringIO()
        code = main(["extract", str(SAMPLES / "lanternfish_LF-0087.html")], env={}, stdout=stdout,
                    stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        data = json.loads(stdout.getvalue())
        self.assertEqual((data["invoice_number"], data["total"]), ("LF-0087", "1350.05"))


if __name__ == "__main__":
    unittest.main()
