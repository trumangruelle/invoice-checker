"""The review queue, driven end to end through the command line.

run -> a person approves one held invoice -> the next run imports it. Every
test here calls main(), the same entry point the terminal calls, and checks
what the CLI printed and what it wrote.
"""

import json
import unittest
from datetime import datetime, timezone

from invoicecheck.cli import EXIT_NEEDS_REVIEW, EXIT_OK, EXIT_USAGE
from invoicecheck.review import NEVER_APPROVABLE, load_approvals, now_stamp, parse_timestamp

from .helpers import CliCase

# mossgate_MG-5498.html: subtotal $936.00 + tax $46.80 = $982.80, but the total says
# $992.80. One finding, total_math, $10.00 of overbilling. Nothing about duplicates,
# so it is an invoice a person can legitimately decide to let through.
HELD = "mossgate_MG-5498.html"


class ReviewQueueTests(CliCase):
    def approve(self, *extra, file=HELD, by="Dana Fell"):
        return self.cli("approve", "--results", self.out / "results.json", "--file", file,
                        "--by", by, "--approvals", self.queue, *extra)

    def queue_entries(self) -> list[dict]:
        return json.loads(self.queue.read_text(encoding="utf-8"))["approvals"]

    # ---------------------------------------------------------------- the happy path

    def test_a_held_invoice_is_approved_and_the_next_run_imports_it(self):
        self.copy_samples(HELD, "bramblewood_BP-10421.txt")
        code, stdout, _ = self.run_cli()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.statuses()[HELD], "NEEDS REVIEW")
        self.assertNotIn("MG-5498", [r["InvoiceNumber"] for r in self.read_csv("import_ready.csv")])

        code, stdout, _ = self.approve("--note", "Agreed fixed price; vendor rounds up.")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Approved mossgate_MG-5498.html (invoice MG-5498, 992.80) by Dana Fell", stdout)
        self.assertIn("it was held for: total is wrong", stdout)
        self.assertIn("reason given: Agreed fixed price; vendor rounds up.", stdout)
        self.assertIn("1 approval in the queue", stdout)

        code, stdout, _ = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.statuses()[HELD], "APPROVED")
        self.assertIn("MG-5498", [r["InvoiceNumber"] for r in self.read_csv("import_ready.csv")])
        self.assertIn("1 PASS, 1 APPROVED, 0 NEEDS REVIEW, 0 NOT CHECKED", stdout)
        self.assertIn("Approved for import: 1 invoice totalling $992.80, including $10.00 of "
                      "overbilling.", stdout)
        self.assertIn("(6 line rows from 1 PASS invoice and 1 a person approved)", stdout)

    def test_the_approval_records_who_when_and_why_it_was_held(self):
        self.copy_samples(HELD)
        self.run_cli()
        before = datetime.now(timezone.utc).replace(microsecond=0)
        self.approve("--note", "Checked against the PO.")
        entries = self.queue_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["file"], HELD)
        self.assertEqual(entry["approved_by"], "Dana Fell")
        self.assertEqual(entry["note"], "Checked against the PO.")
        self.assertEqual(entry["held_for"], ["total_math"])
        self.assertEqual(len(entry["held_reasons"]), 1)
        self.assertIn("[total_math] Subtotal $936.00 + tax $46.80 = $982.80", entry["held_reasons"][0])
        self.assertEqual((entry["vendor"], entry["invoice_number"], entry["total"]),
                         ("Mossgate Janitorial Services", "MG-5498", "992.80"))
        self.assertEqual(len(entry["content_hash"]), 64)
        # The time is real, in UTC, and readable on Python 3.10 (no trailing "Z").
        self.assertFalse(entry["approved_at"].endswith("Z"))
        stamped = parse_timestamp(entry["approved_at"])
        self.assertEqual(stamped.utcoffset(), timezone.utc.utcoffset(None))
        self.assertGreaterEqual(stamped, before)

    def test_who_approved_it_is_in_every_output(self):
        self.copy_samples(HELD)
        self.run_cli()
        self.approve("--note", "Agreed price.")
        self.run_cli("--approvals", self.queue)

        summary = {r["SourceFile"]: r for r in self.read_csv("all_invoices.csv")}[HELD]
        self.assertEqual((summary["Status"], summary["ApprovedBy"]), ("APPROVED", "Dana Fell"))
        self.assertTrue(summary["ApprovedAt"])

        row = self.read_csv("exceptions.csv")[0]
        self.assertEqual((row["SourceFile"], row["Status"], row["Check"], row["ApprovedBy"]),
                         (HELD, "APPROVED", "total_math", "Dana Fell"))

        md = (self.out / "review_report.md").read_text(encoding="utf-8")
        self.assertIn("## Approved by a person (1)", md)
        self.assertIn("| mossgate_MG-5498.html | MG-5498 | $992.80 | Total is wrong | Dana Fell |", md)
        self.assertIn("Agreed price.", md)
        self.assertIn("- Approved for import: 1 invoice totalling $992.80, including $10.00 of "
                      "overbilling\n", md)

        page = (self.out / "review_report.html").read_text(encoding="utf-8")
        self.assertIn("<h2>Approved by a person (1)</h2>", page)
        self.assertIn("<td>Dana Fell</td>", page)
        self.assertIn("approved by a person ($992.80)", page)

        results = self.results()
        self.assertEqual(results["counts"], {"PASS": 0, "APPROVED": 1, "NEEDS REVIEW": 0,
                                             "NOT CHECKED": 0})
        self.assertEqual(results["money"]["approved_overbilling"], "10.00")
        approval = results["invoices"][0]["approval"]
        self.assertEqual((approval["approved_by"], approval["held_for"]), ("Dana Fell", ["total_math"]))

    def test_fail_on_review_passes_once_the_last_held_invoice_is_approved(self):
        self.copy_samples(HELD)
        code, _, _ = self.run_cli("--fail-on-review")
        self.assertEqual(code, EXIT_NEEDS_REVIEW)
        self.approve()
        code, _, _ = self.run_cli("--fail-on-review", "--approvals", self.queue)
        self.assertEqual(code, EXIT_OK)

    # ------------------------------------------- the approval is tied to the content

    def test_an_approval_does_not_carry_over_when_the_invoice_changes(self):
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        path = self.input / HELD
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("992.80"), 1)
        path.write_text(text.replace("992.80", "1992.80"), encoding="utf-8")

        code, stdout, _ = self.run_cli("--approvals", self.queue, "--fail-on-review")
        self.assertEqual(code, EXIT_NEEDS_REVIEW)
        self.assertEqual(self.statuses()[HELD], "NEEDS REVIEW")
        self.assertEqual(self.read_csv("import_ready.csv"), [])
        note = (f"An approval for {HELD} (approved by Dana Fell on "
                f"{now_stamp()[:10]}) was not used: the invoice has changed since it was "
                "approved, so it is held again.")
        self.assertIn(note, stdout)
        self.assertIn(note, (self.out / "review_report.md").read_text(encoding="utf-8"))
        self.assertIn(note, (self.out / "review_report.html").read_text(encoding="utf-8"))
        self.assertEqual(self.results()["invoices"][0]["approval_note"], note)
        row = self.read_csv("exceptions.csv")[0]
        self.assertEqual((row["ApprovedBy"], row["QueueNote"]), ("", note))
        self.assertIn(f"[review_queue] {note}", self.read_csv("all_invoices.csv")[0]["Reasons"])

    def test_an_approval_does_not_cover_a_reason_that_was_not_there_when_it_was_given(self):
        # Approved for total_math on 2026-09-21. Run again two years later and the invoice
        # is also stale, which nobody approved, so it is held again and says so.
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        code, stdout, _ = self.run_cli("--approvals", self.queue, today="2028-09-21")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.statuses()[HELD], "NEEDS REVIEW")
        self.assertIn("for total is wrong, but this run also found very old invoice, which that "
                      "approval does not cover.", stdout)
        self.assertEqual(self.read_csv("import_ready.csv"), [])

    def test_a_file_renamed_and_changed_still_says_its_approval_was_dropped(self):
        # Neither the hash nor the file name matches any more, so the vendor and invoice
        # number are what connect the two. Without that the approval would vanish silently.
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        text = (self.input / HELD).read_text(encoding="utf-8")
        (self.input / HELD).unlink()
        (self.input / "mg5498_v2.html").write_text(text.replace("992.80", "1992.80"),
                                                   encoding="utf-8")
        code, stdout, _ = self.run_cli("--approvals", self.queue, "--fail-on-review")
        self.assertEqual(code, EXIT_NEEDS_REVIEW)
        self.assertEqual(self.statuses(), {"mg5498_v2.html": "NEEDS REVIEW"})
        self.assertIn(f"An approval for {HELD} (approved by Dana Fell on {now_stamp()[:10]}) was "
                      "not used: the invoice has changed since it was approved, so it is held "
                      "again.", stdout)

    def test_the_same_invoice_under_another_name_uses_the_same_approval(self):
        # The hash is of the invoice, not the file name, so renaming it does not void the
        # approval -- and the duplicate check below is what stops the copy being paid twice.
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        (self.input / HELD).rename(self.input / "renamed.html")
        code, _, _ = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.statuses()["renamed.html"], "APPROVED")

    # -------------------------------------------- an approval never pays twice

    def test_approve_refuses_an_invoice_that_may_already_have_been_paid(self):
        self.copy_samples("mossgate_MG-5512.html", "mossgate_MG-5512_reminder.html")
        self.run_cli()
        code, stdout, stderr = self.approve(file="mossgate_MG-5512.html")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("is held because it may already have been paid (sent twice), and approving "
                      "it cannot release it", stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(self.queue.exists(), "nothing may be written to the queue")

    def test_a_copy_that_turns_up_later_still_holds_an_approved_invoice(self):
        # Approved when it was alone in the folder; a second copy arrives next month.
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        (self.input / "mg5498_reminder.html").write_text(
            (self.input / HELD).read_text(encoding="utf-8"), encoding="utf-8")

        code, stdout, _ = self.run_cli("--approvals", self.queue, "--fail-on-review")
        self.assertEqual(code, EXIT_NEEDS_REVIEW)
        self.assertEqual(set(self.statuses().values()), {"NEEDS REVIEW"})
        self.assertEqual(self.read_csv("import_ready.csv"), [])
        self.assertIn("but still held: sent twice. An approval never clears a possible duplicate",
                      stdout)

    # ------------------------------------- the line names a person, so it spells them right

    def test_the_held_copy_names_the_approver_exactly_as_they_were_entered(self):
        # This is the sentence the README quotes, and it is an audit line: its whole
        # job is to say who approved the invoice. str.capitalize() was here, which
        # upper-cases the first letter *and lowercases everything after it*, so it
        # printed "Approved by dana fell". The tail of the sentence was asserted and
        # the name was not, which is why nothing went red.
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        (self.input / "mg5498_reminder.html").write_text(
            (self.input / HELD).read_text(encoding="utf-8"), encoding="utf-8")

        code, stdout, _ = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_OK)
        note = (f"Approved by Dana Fell on {now_stamp()[:10]}, but still held: sent twice. "
                f"An approval never clears a possible duplicate, because paying an invoice "
                f"twice is the mistake this check exists to stop.")
        self.assertIn(note, stdout)
        notes = {i["invoice"]["source_file"]: i["approval_note"]
                 for i in self.results()["invoices"]}
        self.assertEqual(notes[HELD], note)
        self.assertIn(note, (self.out / "review_report.md").read_text(encoding="utf-8"))
        self.assertIn(note, (self.out / "review_report.html").read_text(encoding="utf-8"))
        self.assertEqual({r["QueueNote"] for r in self.read_csv("exceptions.csv")
                          if r["SourceFile"] == HELD}, {note})

    def test_an_uncovered_reason_names_the_approver_exactly_as_they_were_entered(self):
        # The other sentence built the same way. A name that capitalize() mangles
        # beyond the first word, so a partial fix would still be caught here.
        self.copy_samples(HELD)
        self.run_cli()
        self.approve(by="J. P. MacLeod III")
        code, stdout, _ = self.run_cli("--approvals", self.queue, today="2028-09-21")
        self.assertEqual(code, EXIT_OK)
        note = (f"Approved by J. P. MacLeod III on {now_stamp()[:10]} for total is wrong, but "
                f"this run also found very old invoice, which that approval does not cover.")
        self.assertIn(note, stdout)
        self.assertEqual(self.results()["invoices"][0]["approval_note"], note)
        self.assertIn(note, (self.out / "review_report.md").read_text(encoding="utf-8"))
        self.assertIn(note, (self.out / "review_report.html").read_text(encoding="utf-8"))

    def test_a_queue_file_edited_to_approve_a_duplicate_stops_the_run(self):
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        data = json.loads(self.queue.read_text(encoding="utf-8"))
        data["approvals"][0]["held_for"] = ["total_math", "duplicate_number"]
        self.queue.write_text(json.dumps(data), encoding="utf-8")
        code, _, stderr = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("this approval lists duplicate_number, which an approval can never clear",
                      stderr)

    def test_every_never_approvable_check_is_a_real_check(self):
        from invoicecheck.verify import CHECKS
        for check in NEVER_APPROVABLE:
            self.assertIn(check, CHECKS)

    # -------------------------------------------------------- the approve command itself

    def test_approving_an_invoice_that_passed_is_refused(self):
        self.copy_samples("bramblewood_BP-10421.txt")
        self.run_cli()
        code, _, stderr = self.approve(file="bramblewood_BP-10421.txt")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("passed every check in that run, so there is nothing to approve", stderr)
        self.assertFalse(self.queue.exists())

    def test_a_file_name_that_is_not_in_the_run_lists_the_held_invoices(self):
        self.copy_samples(HELD, "kestrel_KR-2304.txt")
        self.run_cli()
        code, _, stderr = self.approve(file="mossgate_MG-5499.html")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no invoice named 'mossgate_MG-5499.html'", stderr)
        self.assertIn("kestrel_KR-2304.txt", stderr)
        self.assertIn("mossgate_MG-5498.html", stderr)

    def test_an_empty_name_is_refused_so_no_approval_is_anonymous(self):
        self.copy_samples(HELD)
        self.run_cli()
        code, _, stderr = self.approve(by="   ")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--by must say who is approving it", stderr)
        self.assertFalse(self.queue.exists())

    def test_approving_the_same_invoice_twice_is_refused(self):
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        self.run_cli("--approvals", self.queue)
        code, _, stderr = self.approve(by="Sam Rill")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("was already approved by Dana Fell", stderr)
        self.assertEqual(len(self.queue_entries()), 1)

    def test_a_queue_path_that_cannot_be_written_says_so_rather_than_crashing(self):
        self.copy_samples(HELD)
        self.run_cli()
        self.queue.mkdir()   # a folder where the queue file should be
        code, stdout, stderr = self.approve()
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("could not write the review queue", stderr)
        self.assertIn("nothing was recorded", stderr)
        self.assertEqual(stdout, "")

    def test_a_missing_results_file_says_to_run_the_check_first(self):
        code, _, stderr = self.approve()
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("results file not found", stderr)
        self.assertIn("invoicecheck run", stderr)

    def test_a_second_approval_is_added_to_the_queue_not_written_over_it(self):
        self.copy_samples(HELD, "kestrel_KR-2304.txt")
        self.run_cli()
        self.approve()
        self.approve(file="kestrel_KR-2304.txt", by="Sam Rill")
        entries = self.queue_entries()
        self.assertEqual([e["file"] for e in entries], [HELD, "kestrel_KR-2304.txt"])
        self.assertEqual([e["approved_by"] for e in entries], ["Dana Fell", "Sam Rill"])
        code, stdout, _ = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("0 PASS, 2 APPROVED, 0 NEEDS REVIEW", stdout)

    def test_a_free_text_note_with_a_line_break_does_not_break_the_csvs(self):
        self.copy_samples(HELD)
        self.run_cli()
        self.approve("--note", "Spoke to the vendor.\nThey will credit it next month.")
        self.run_cli("--approvals", self.queue)
        row = self.read_csv("exceptions.csv")[0]
        self.assertEqual(row["ApprovedReason"], "Spoke to the vendor.\nThey will credit it next month.")
        self.assertEqual(row["QueueNote"], "")
        # The line break is inside a quoted cell: the file still has a header and one row.
        self.assertEqual(len(self.read_rows("exceptions.csv")), 2)
        # In the Markdown report the same note is folded onto one line, or it would end
        # the table row halfway through.
        md = (self.out / "review_report.md").read_text(encoding="utf-8")
        self.assertIn("| Spoke to the vendor. They will credit it next month. |", md)

    # -------------------------------------------------------- the queue file itself

    def test_a_queue_file_that_is_not_json_stops_the_run_before_anything_is_written(self):
        self.copy_samples(HELD)
        self.queue.write_text("{oops", encoding="utf-8")
        code, stdout, stderr = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("it is not valid JSON (line 1, column", stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(self.out.exists())

    def test_a_queue_file_missing_a_field_names_which_approval_is_wrong(self):
        self.copy_samples(HELD)
        for entry, expected in (
                ({"content_hash": "a" * 64, "approved_by": "D", "approved_at": now_stamp()},
                 "approval 1: 'file' must be a non-empty string"),
                ({"file": "x", "content_hash": "zz", "approved_by": "D", "approved_at": now_stamp()},
                 "approval 1: 'content_hash' must be 64 hexadecimal characters"),
                ({"file": "x", "content_hash": "a" * 64, "approved_by": "D", "approved_at": "soon"},
                 "approval 1: 'approved_at' is not a date and time I can read"),
                ({"file": "x", "content_hash": "a" * 64, "approved_by": "", "approved_at": now_stamp()},
                 "approval 1: 'approved_by' must be a non-empty string")):
            with self.subTest(entry=entry):
                self.queue.write_text(json.dumps({"approvals": [entry]}), encoding="utf-8")
                code, _, stderr = self.run_cli("--approvals", self.queue)
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn(expected, stderr)

    def test_a_queue_file_that_is_not_a_queue_says_so(self):
        self.copy_samples(HELD)
        self.queue.write_text(json.dumps([{"file": "x"}]), encoding="utf-8")
        code, _, stderr = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn('it must be a JSON object with an "approvals" list', stderr)

    def test_a_missing_queue_file_says_how_to_make_one(self):
        self.copy_samples(HELD)
        code, _, stderr = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("review queue not found", stderr)
        self.assertIn("python -m invoicecheck approve", stderr)

    def test_a_timestamp_ending_in_z_is_read_not_rejected(self):
        # datetime.fromisoformat() before Python 3.11 cannot read a trailing "Z", and the
        # floor for this project is 3.10. A queue file carrying one still has to work.
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        data = json.loads(self.queue.read_text(encoding="utf-8"))
        data["approvals"][0]["approved_at"] = "2026-09-22T09:15:00Z"
        self.queue.write_text(json.dumps(data), encoding="utf-8")
        code, stdout, stderr = self.run_cli("--approvals", self.queue)
        self.assertEqual(code, EXIT_OK, stderr)
        self.assertEqual(self.statuses()[HELD], "APPROVED")
        self.assertIn("2026-09-22T09:15:00Z", (self.out / "review_report.md").read_text(encoding="utf-8"))

    def test_the_queue_is_written_in_the_documented_shape(self):
        self.copy_samples(HELD)
        self.run_cli()
        self.approve()
        data = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(sorted(data["approvals"][0]),
                         ["approved_at", "approved_by", "content_hash", "file", "held_for",
                          "held_reasons", "invoice_number", "note", "total", "vendor"])
        # And it reads back as the same approvals.
        self.assertEqual([a.file for a in load_approvals(self.queue)], [HELD])


if __name__ == "__main__":
    unittest.main()
