"""Command line: python -m invoicecheck run --input samples/ --out out/"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, TextIO

from . import extract_ai, extract_rules
from .documents import Document, NotReadable, load_document, load_folder
from .importmap import MappingError, load_mapping
from .model import Invoice
from .outputs import (
    IMPORT_COLUMNS,
    approved_line,
    held_line,
    import_contents_line,
    overbilling_line,
    result_line,
    sent_twice_line,
    write_exceptions_csv,
    write_import_csv,
    write_mapped_import_csv,
    write_results_json,
    write_review_html,
    write_review_report,
    write_summary_csv,
)
from .review import (
    NEVER_APPROVABLE,
    Approval,
    ApprovalsError,
    add_approval,
    apply_approvals,
    load_approvals,
    now_stamp,
)
from .textutil import money_str
from .verify import (LABELS, NOT_CHECKED, PASS, HistoryError, load_history, money_summary,
                     verify_all)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NEEDS_REVIEW = 3

AI_NEEDS_KEY = (f"--extractor ai is the optional AI step, and it runs on your own Anthropic API key: "
                f"set {extract_ai.ENV_KEY} in the environment")


def display_path(path: Path) -> str:
    """A path for printing, relative to the working directory ("out/...", "../reports/...").

    Output pasted into a README or a ticket then does not carry the local
    user folder, even when --out was given as an absolute path. A folder on
    another drive has no relative path, so it is printed as given.
    """
    try:
        return Path(os.path.relpath(path.resolve(), Path.cwd().resolve())).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


_YYYY_MM_DD = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_arg(text: str) -> date:
    """--today, pinned to YYYY-MM-DD on every version this runs on.

    The shape is checked here instead of being left to date.fromisoformat(),
    which Python 3.11 widened to read "20260923", "2026-W39-3" and more. The
    floor is 3.10, where only "YYYY-MM-DD" is accepted, so without this a
    developer's newer Python would take a date string that the client's Ubuntu
    22.04 rejects. Both now behave the same way, which is the stricter one, and
    the one the help text and the README document.
    """
    if not _YYYY_MM_DD.fullmatch(text):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m invoicecheck",
        description="Extract invoice data and verify it before it goes into the books.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="extract and verify every invoice in a folder")
    run.add_argument("--input", required=True, type=Path,
                     help="folder of invoices: .txt, .html and .pdf files (PDFs need pypdf) are checked, "
                          "any other file is listed as NOT CHECKED")
    run.add_argument("--out", required=True, type=Path, help="folder for the CSVs and the report")
    run.add_argument("--extractor", choices=("rules", "ai", "auto"), default="rules",
                     help="rules (default, offline). The optional AI step runs on your own Anthropic "
                          f"API key ({extract_ai.ENV_KEY}): ai sends every file to the Messages API; "
                          "auto uses the rules first and AI only for files they could not fully read")
    run.add_argument("--today", type=_date_arg, default=None,
                     help="date to check invoice dates against (default: today)")
    run.add_argument("--max-age-days", type=int, default=365,
                     help="flag invoices older than this many days; 0 turns the check off")
    run.add_argument("--history", type=Path, default=None,
                     help="CSV from an earlier run; invoices already in it are flagged")
    run.add_argument("--import-map", type=Path, default=None, metavar="FILE",
                     help="JSON file naming and ordering the columns your accounting software "
                          "imports; also writes import_ready_mapped.csv with those columns")
    run.add_argument("--approvals", type=Path, default=None, metavar="FILE",
                     help="the review queue written by 'approve'; a held invoice a person "
                          "approved is added to the import file")
    run.add_argument("--model", default=None,
                     help=f"model for the optional AI extractor (default {extract_ai.DEFAULT_MODEL})")
    run.add_argument("--fail-on-review", action="store_true",
                     help=f"exit with status {EXIT_NEEDS_REVIEW} if any invoice needs review "
                          f"or any file could not be checked")

    one = sub.add_parser("extract", help="print what the extractor reads from one file, as JSON")
    one.add_argument("file", type=Path)
    one.add_argument("--extractor", choices=("rules", "ai"), default="rules",
                     help="rules (default, offline); ai is optional and runs on your own Anthropic API key")
    one.add_argument("--model", default=None)

    ok = sub.add_parser("approve", help="mark one held invoice approved, so the next run imports it")
    ok.add_argument("--results", required=True, type=Path, metavar="FILE",
                    help="results.json from the run that held it (the run the person reviewed)")
    ok.add_argument("--file", required=True, metavar="NAME",
                    help="the invoice's file name, exactly as the report shows it")
    ok.add_argument("--by", required=True, metavar="NAME",
                    help="who is approving it: free text, recorded in the queue with the time")
    ok.add_argument("--approvals", required=True, type=Path, metavar="FILE",
                    help="the review queue file to add it to (created if it does not exist)")
    ok.add_argument("--note", default="", metavar="TEXT",
                    help="why it is being approved; recorded with the approval")
    return parser


def extract_document(doc: Document, mode: str, *, api_key: str | None, model: str | None,
                     opener: Callable | None = None, sleep: Callable | None = None) -> Invoice:
    """Extract one document. A problem reading the file itself is kept as a warning on the result."""
    invoice = _extract(doc, mode, api_key=api_key, model=model, opener=opener, sleep=sleep)
    invoice.warnings[:0] = doc.warnings
    return invoice


def _extract(doc: Document, mode: str, *, api_key: str | None, model: str | None,
             opener: Callable | None, sleep: Callable | None) -> Invoice:
    model = model or extract_ai.DEFAULT_MODEL
    ai_kwargs = {"model": model, "opener": opener}
    if sleep is not None:
        ai_kwargs["sleep"] = sleep

    if mode == "ai":
        try:
            return extract_ai.extract_with_ai(doc, api_key, **ai_kwargs)
        except extract_ai.AIExtractionError as err:
            return Invoice(source_file=doc.name, layout="ai", extractor="ai",
                           warnings=[f"AI extraction failed: {err}"])

    invoice = extract_rules.extract(doc)
    if mode != "auto" or (invoice.layout != "unrecognised" and not invoice.warnings):
        return invoice
    if not api_key:
        invoice.warnings.append(f"AI fallback skipped because {extract_ai.ENV_KEY} is not set")
        return invoice
    try:
        return extract_ai.extract_with_ai(doc, api_key, **ai_kwargs)
    except extract_ai.AIExtractionError as err:
        invoice.warnings.append(f"AI fallback failed: {err}")
        return invoice


def _cmd_run(args, env: Mapping[str, str], opener, sleep, out: TextIO, err: TextIO) -> int:
    if not args.input.is_dir():
        print(f"error: input folder not found: {args.input}", file=err)
        return EXIT_USAGE
    api_key = env.get(extract_ai.ENV_KEY) or None
    if args.extractor == "ai" and not api_key:
        print(f"error: {AI_NEEDS_KEY}; use --extractor rules to run offline", file=err)
        return EXIT_USAGE

    # The mapping and the review queue are checked before any invoice is read: a typo in
    # either changes what reaches the accounting software, so it stops the run at once
    # rather than writing a file with a column missing or an approval quietly not applied.
    mapping = None
    if args.import_map is not None:
        if not args.import_map.is_file():
            print(f"error: import map not found: {args.import_map}", file=err)
            return EXIT_USAGE
        try:
            mapping = load_mapping(args.import_map, IMPORT_COLUMNS)
        except MappingError as problem:
            print(f"error: import map {args.import_map}: {problem}", file=err)
            return EXIT_USAGE
    approvals = []
    if args.approvals is not None:
        if not args.approvals.is_file():
            print(f"error: review queue not found: {args.approvals}; it is created the first time "
                  f"you run 'python -m invoicecheck approve'", file=err)
            return EXIT_USAGE
        try:
            approvals = load_approvals(args.approvals)
        except ApprovalsError as problem:
            print(f"error: review queue {args.approvals}: {problem}", file=err)
            return EXIT_USAGE
    # --out is checked here for the same reason, and nothing is created yet: the run
    # writes six files into that folder at the end, so a path that cannot hold them
    # should stop the run now rather than after every invoice has been extracted --
    # which, with --extractor ai, is after the API calls have been paid for.
    if args.out.exists() and not args.out.is_dir():
        print(f"error: --out {args.out} is a file, not a folder; give a folder name, "
              f"or move that file out of the way", file=err)
        return EXIT_USAGE

    docs, unread = load_folder(args.input)
    if not docs:
        listed = "".join(f"\n  {NOT_CHECKED}  {u.name}  ({u.reason})" for u in unread)
        print(f"error: no invoices it could read in {args.input}{listed}", file=err)
        return EXIT_USAGE
    history = None
    if args.history is not None:
        if not args.history.is_file():
            print(f"error: history file not found: {args.history}", file=err)
            return EXIT_USAGE
        try:
            history = load_history(args.history)
        except HistoryError as problem:
            print(f"error: history file {args.history}: {problem}", file=err)
            return EXIT_USAGE

    today = args.today or date.today()
    invoices = [extract_document(d, args.extractor, api_key=api_key, model=args.model,
                                 opener=opener, sleep=sleep) for d in docs]
    verdicts = verify_all(invoices, today=today, max_age_days=args.max_age_days,
                          sources={d.name: d.text for d in docs}, history=history)
    apply_approvals(verdicts, approvals)

    try:
        # exist_ok covers a folder that is already there; it does not cover a file
        # sitting on that path, nor a file part-way up it, nor a folder that cannot
        # be created. Any of those used to reach the user as a traceback.
        args.out.mkdir(parents=True, exist_ok=True)
    except OSError as problem:
        print(f"error: could not create the output folder {args.out} "
              f"({problem.strerror or problem}); nothing was written", file=err)
        return EXIT_USAGE
    label = args.input.resolve().name or str(args.input)
    report = {"today": today, "input_label": label, "extractor": args.extractor, "unread": unread}
    built = write_import_csv(verdicts, args.out / "import_ready.csv")
    write_summary_csv(verdicts, args.out / "all_invoices.csv", unread=unread)
    exceptions = write_exceptions_csv(verdicts, args.out / "exceptions.csv", unread=unread)
    write_review_html(verdicts, args.out / "review_report.html", **report)
    write_review_report(verdicts, args.out / "review_report.md", **report)
    write_results_json(verdicts, args.out / "results.json", **report)
    if mapping is not None:
        write_mapped_import_csv(built, mapping, args.out / "import_ready_mapped.csv")

    width = max(len(name) for name in [v.invoice.source_file for v in verdicts] + [u.name for u in unread])
    not_read = ""
    if unread:
        not_read = f", {len(unread)} other {'file' if len(unread) == 1 else 'files'} not checked"
    print(f"invoicecheck: {len(verdicts)} {'invoice' if len(verdicts) == 1 else 'invoices'} from {label}/{not_read} "
          f"(extractor: {args.extractor}, dates checked against {today.isoformat()})", file=out)
    for v in verdicts:
        inv = v.invoice
        total = money_str(inv.total) or "-"
        detail = ", ".join(dict.fromkeys(LABELS[f.check] for f in v.findings))
        line = f"  {v.status:<12}  {inv.source_file:<{width}}  {inv.invoice_number or '-':<10} {total:>10}"
        print(line + (f"  {detail}" if detail else ""), file=out)
    for u in unread:
        print(f"  {NOT_CHECKED:<12}  {u.name:<{width}}  {'-':<10} {'-':>10}  {u.reason}", file=out)
    # An approval that was on file but could not be used is said out loud, here and in
    # every report. The quiet version of this is an invoice sliding through unchecked.
    for v in verdicts:
        if v.approval_note:
            print(f"  review queue: {v.invoice.source_file}: {v.approval_note}", file=out)
    print(result_line(verdicts, unread), file=out)
    money = money_summary(verdicts)
    print(f"{held_line(money)}. {overbilling_line(money)}.", file=out)
    twice = sent_twice_line(money)
    if twice:
        print(f"{twice}.", file=out)
    letthrough = approved_line(money)
    if letthrough:
        print(f"{letthrough}.", file=out)
    rows = len(built.rows)
    counted = f"{built.passed} PASS {'invoice' if built.passed == 1 else 'invoices'}"
    if built.approved:
        counted += f" and {built.approved} a person approved"
    print(f"Wrote {display_path(args.out / 'import_ready.csv')} ({rows} line "
          f"{'row' if rows == 1 else 'rows'} from {counted})", file=out)
    print(f"Wrote {display_path(args.out / 'all_invoices.csv')}", file=out)
    print(f"Wrote {display_path(args.out / 'exceptions.csv')} ({exceptions} "
          f"{'row' if exceptions == 1 else 'rows'}, one per flag)", file=out)
    for name in ("review_report.html", "review_report.md", "results.json"):
        print(f"Wrote {display_path(args.out / name)}", file=out)
    if mapping is not None:
        named = f' for "{mapping.template}"' if mapping.template else ""
        print(f"Wrote {display_path(args.out / 'import_ready_mapped.csv')} "
              f"({len(mapping.targets)} columns{named})", file=out)
        constants = mapping.constants()
        if constants:
            print(f"  constant columns: {', '.join(constants)}", file=out)
        # Columns the mapping does not use are left out on purpose, but never silently.
        left_out = mapping.left_out(IMPORT_COLUMNS)
        if left_out:
            print(f"  left out of the mapped file: {', '.join(left_out)}", file=out)
        print(f"  {import_contents_line(money)} import_ready.csv keeps this tool's own column "
              f"names; --history and the review queue read that one.", file=out)

    # An approved invoice has already had a person look at it, so it does not hold the job up.
    unresolved = sum(not v.importable for v in verdicts)
    if args.fail_on_review and (unresolved or unread):
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


def _cmd_approve(args, out: TextIO, err: TextIO) -> int:
    """Record one person's approval of one held invoice, tied to that invoice's content."""
    who = args.by.strip()
    if not who:
        print("error: --by must say who is approving it; it goes in the queue as the "
              "person who checked this invoice", file=err)
        return EXIT_USAGE
    if not args.results.is_file():
        print(f"error: results file not found: {args.results}; run "
              f"'python -m invoicecheck run --input ... --out ...' first", file=err)
        return EXIT_USAGE
    try:
        results = json.loads(args.results.read_text(encoding="utf-8-sig"))
        entries = results["invoices"]
    except (json.JSONDecodeError, KeyError, TypeError, OSError) as problem:
        print(f"error: {args.results} is not a results.json this tool wrote ({problem})", file=err)
        return EXIT_USAGE

    found = [e for e in entries if e.get("invoice", {}).get("source_file") == args.file]
    if not found:
        holdable = [e["invoice"]["source_file"] for e in entries if e.get("status") != PASS]
        listed = "".join(f"\n  {name}" for name in holdable) or "\n  (nothing in that run was held)"
        print(f"error: no invoice named {args.file!r} in {args.results}. Held invoices in that "
              f"run:{listed}", file=err)
        return EXIT_USAGE
    entry = found[0]
    findings = entry.get("findings") or []
    if not findings:
        print(f"error: {args.file} passed every check in that run, so there is nothing to "
              f"approve; it is already in the import file", file=err)
        return EXIT_USAGE

    checks = list(dict.fromkeys(f["check"] for f in findings))
    # The first of three guards on paying an invoice twice: an approval that could never
    # be used is not written at all, so nobody is left believing a possible double payment
    # has been dealt with. The second is in review.apply_approvals, for a duplicate that
    # only turns up in a later batch; the third is in review.load_approvals, which rejects
    # a queue file edited by hand to list one of these checks.
    blocked = [c for c in checks if c in NEVER_APPROVABLE]
    if blocked:
        labels = ", ".join(dict.fromkeys(LABELS[c] for c in blocked))
        print(f"error: {args.file} is held because it may already have been paid ({labels}), and "
              f"approving it cannot release it. Check whether the earlier copy was paid, then "
              f"either remove this copy from the folder or take the earlier one out of the "
              f"--history file. Nothing was written to {args.approvals}.", file=err)
        return EXIT_USAGE

    digest = entry.get("content_hash")
    if not isinstance(digest, str) or not digest:
        print(f"error: {args.results} has no content_hash for {args.file}; it was written by an "
              f"older version. Run the check again and approve from the new results.json.", file=err)
        return EXIT_USAGE
    if entry.get("approval"):
        was = entry["approval"]
        print(f"error: {args.file} was already approved by {was.get('approved_by')} on "
              f"{was.get('approved_at')}", file=err)
        return EXIT_USAGE

    inv = entry["invoice"]
    approval = Approval(
        file=args.file,
        content_hash=digest,
        approved_by=who,
        approved_at=now_stamp(),
        held_for=tuple(checks),
        held_reasons=tuple(f"[{f['check']}] {f['message']}" for f in findings),
        vendor=inv.get("vendor") or "",
        invoice_number=inv.get("invoice_number") or "",
        total=inv.get("total") or "",
        note=args.note.strip(),
    )
    try:
        queue = add_approval(args.approvals, approval)
    except ApprovalsError as problem:
        print(f"error: review queue {args.approvals}: {problem}", file=err)
        return EXIT_USAGE
    except OSError as problem:
        print(f"error: could not write the review queue {args.approvals} "
              f"({problem.strerror or problem}); nothing was recorded", file=err)
        return EXIT_USAGE

    labels = ", ".join(dict.fromkeys(LABELS[c] for c in checks))
    print(f"Approved {args.file} (invoice {approval.invoice_number or '-'}, "
          f"{approval.total or '-'}) by {who} at {approval.approved_at}", file=out)
    print(f"  it was held for: {labels}", file=out)
    if approval.note:
        print(f"  reason given: {approval.note}", file=out)
    print(f"  written to {display_path(args.approvals)} ({len(queue)} "
          f"{'approval' if len(queue) == 1 else 'approvals'} in the queue)", file=out)
    print(f"  the next run with --approvals {display_path(args.approvals)} puts it in the import "
          f"file, unless the invoice has changed since", file=out)
    return EXIT_OK


def _cmd_extract(args, env, opener, sleep, out: TextIO, err: TextIO) -> int:
    if not args.file.is_file():
        print(f"error: file not found: {args.file}", file=err)
        return EXIT_USAGE
    api_key = env.get(extract_ai.ENV_KEY) or None
    if args.extractor == "ai" and not api_key:
        print(f"error: {AI_NEEDS_KEY}", file=err)
        return EXIT_USAGE
    try:
        doc = load_document(args.file)
    except NotReadable as problem:
        print(f"{NOT_CHECKED}: {args.file.name}: {problem.reason}", file=err)
        return EXIT_USAGE
    invoice = extract_document(doc, args.extractor, api_key=api_key, model=args.model,
                               opener=opener, sleep=sleep)
    print(json.dumps(invoice.to_dict(), indent=2), file=out)
    return EXIT_OK


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
         opener: Callable | None = None, sleep: Callable | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    for stream in (out, err):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - already detached, etc.
                pass
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    if args.command == "run":
        return _cmd_run(args, env, opener, sleep, out, err)
    if args.command == "approve":
        return _cmd_approve(args, out, err)
    return _cmd_extract(args, env, opener, sleep, out, err)
