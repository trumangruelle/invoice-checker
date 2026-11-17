"""Mutation check: break one rule at a time and confirm the tests notice.

A green test suite only means something if it goes red when the code is
wrong. This copies the project to a temporary folder, applies each mutation
below in turn, runs the suite, and reports any mutant the tests fail to kill.
A mutation whose target text is not found exactly once counts as a survivor,
so a refactor cannot quietly turn a mutant into a no-op.

    python tools/mutation_check.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

VERIFY = "invoicecheck/verify.py"
RULES = "invoicecheck/extract_rules.py"
AI = "invoicecheck/extract_ai.py"
DOCS = "invoicecheck/documents.py"
CLI = "invoicecheck/cli.py"
OUTPUTS = "invoicecheck/outputs.py"
PDF = "invoicecheck/pdftext.py"
REVIEW = "invoicecheck/review.py"
MAP = "invoicecheck/importmap.py"
MODEL = "invoicecheck/model.py"

# The floor mutants below ship a 3.11+ name in the package. They are written as an
# insertion after the one line every module in the package starts with, so each one
# is a real import in a real module, exactly as the mistake would arrive.
FUTURE = "from __future__ import annotations\n"

MUTANTS = [
    # --- verification rules
    ("line_math tolerance widened", VERIFY,
     "        if abs(item.amount - expected) > CENT:", "        if abs(item.amount - expected) > CENT * 1000:"),
    ("lines_sum disabled", VERIFY,
     "        if abs(lines - inv.subtotal) > CENT:", "        if False:"),
    ("tax_rate disabled", VERIFY,
     "    if abs(inv.tax - expected) > tolerance:", "    if abs(inv.tax - expected) > tolerance * 1000:"),
    ("total_math disabled", VERIFY,
     "    if abs(inv.total - expected) > CENT:", "    if abs(inv.total - expected) > CENT * 1000:"),
    ("zero total allowed (<= -> <)", VERIFY,
     "    if inv.total is not None and inv.total <= 0:", "    if inv.total is not None and inv.total < 0:"),
    ("future date allowed", VERIFY,
     "        if issued > today:", "        if issued > today.replace(year=today.year + 100):"),
    ("stale limit shifted by 30 days", VERIFY,
     "        elif max_age_days > 0 and (today - issued).days > max_age_days:",
     "        elif max_age_days > 0 and (today - issued).days > max_age_days + 30:"),
    ("duplicates need three copies", VERIFY,
     "        if len(indexes) < 2:", "        if len(indexes) < 3:"),
    ("same number under another vendor name not flagged", VERIFY,
     "                if same:", "                if False:"),
    ("grounding disabled", VERIFY,
     "    printed = numbers_in(source_text)", "    return []\n    printed = numbers_in(source_text)"),
    ("grounding ignores quantities", VERIFY,
     '        require(f"item {n} quantity", item.quantity)', "        pass"),
    ("extractor warnings ignored", VERIFY,
     '    return [Finding("extraction", f"Extractor note: {w}.") for w in inv.warnings]', "    return []"),
    # --- overbilling figure
    ("undercharges counted as overbilling", VERIFY,
     "        return max(net, ZERO)", "        return abs(net)"),
    ("every copy of a duplicate counted as a second copy", VERIFY,
     "            repeat = position > 0 and inv.total is not None and inv.total > 0",
     "            repeat = inv.total is not None and inv.total > 0"),
    ("invoices sent twice added into the overbilling figure", VERIFY,
     "    overcharge = sum((v.overcharge for v in held), ZERO)",
     "    overcharge = sum((v.overcharge + v.repeat_total for v in held), ZERO)"),
    ("an arithmetic error sent twice counted on both copies", VERIFY,
     "        if self.is_repeat_copy:\n            return ZERO\n", ""),
    ("overbilling counted on an invoice that was not fully read", VERIFY,
     "        if any(f.check in UNTRUSTED_FIGURES for f in self.findings):", "        if False:"),
    # --- files that cannot be read
    ("file it cannot read dropped silently", DOCS,
     "            unread.append(Unread(relative.as_posix(), unread_reason(path)))", "            pass"),
    ("hidden files read instead of skipped", DOCS,
     "        if _ignored(relative):\n", "        if False:\n"),
    ("PDF it cannot read dropped silently", DOCS,
     "            unread.append(Unread(path.relative_to(folder).as_posix(), err.reason))", "            pass"),
    ("pypdf imported even when there is no PDF", DOCS,
     "from .pdftext import NotReadable, pdf_text\n",
     "from .pdftext import NotReadable, pdf_text, load_pypdf as _load; _load()\n"),
    # --- PDF text layer
    ("missing pypdf reported with the wrong reason", PDF,
     "        raise NotReadable(NEEDS_PYPDF)", "        raise NotReadable(SCANNED)"),
    ("scanned PDF read as an empty invoice", PDF,
     "    if not text.strip():\n        raise NotReadable(SCANNED)", "    if False:\n        raise NotReadable(SCANNED)"),
    ("PDF read in plain mode, losing the columns", PDF,
     '        return page.extract_text(extraction_mode="layout")', "        return page.extract_text()"),
    ("pypdf warnings printed for a damaged PDF", PDF,
     "    logger.setLevel(logging.CRITICAL)", "    pass"),
    ("password-protected PDF reported as damaged", PDF,
     "            except pypdf.errors.FileNotDecryptedError:", "            except KeyError:"),
    ("PDF needing another package reported as damaged", PDF,
     "            except pypdf.errors.DependencyError as err:", "            except KeyError as err:"),
    ("undecodable bytes replaced without a warning", DOCS,
     '        return text, [NOT_UTF8_WARNING.format(count=text.count("\\ufffd"))]', "        return text, []"),
    ("file-reading warnings not carried onto the invoice", CLI,
     "    invoice.warnings[:0] = doc.warnings", "    pass"),
    ("--fail-on-review ignores files not checked", CLI,
     "    if args.fail_on_review and (unresolved or unread):",
     "    if args.fail_on_review and unresolved:"),
    ("--fail-on-review still fails on an invoice a person approved", CLI,
     "    unresolved = sum(not v.importable for v in verdicts)",
     "    unresolved = sum(v.status != PASS for v in verdicts)"),
    ("files not checked left out of the summary CSV", OUTPUTS,
     '        for item in unread:\n            writer.writerow([safe_text(item.name), NOT_CHECKED, *[""]',
     '        for item in ():\n            writer.writerow([safe_text(item.name), NOT_CHECKED, *[""]'),
    # --- outputs
    ("review invoices leak into import CSV", OUTPUTS,
     "        if not verdict.importable:", "        if False:"),
    ("an approved invoice is left out of the import CSV", OUTPUTS,
     "        if not verdict.importable:", "        if verdict.status != PASS:"),
    ("unit price rounded to cents in import CSV", OUTPUTS,
     "fmt_number(item.quantity), price_str(item.unit_price), money_str(item.amount),",
     "fmt_number(item.quantity), money_str(item.unit_price), money_str(item.amount),"),
    ("HTML report does not escape invoice text", OUTPUTS,
     "from html import escape", "escape = str"),
    ("banker's rounding", "invoicecheck/textutil.py",
     "    return value.quantize(CENT, rounding=ROUND_HALF_UP)", "    return value.quantize(CENT)"),
    ("absolute --out path printed as given", CLI,
     "        return Path(os.path.relpath(path.resolve(), Path.cwd().resolve())).as_posix()",
     "        return path.as_posix()"),
    ("--out outside the working directory printed as given", CLI,
     "        return Path(os.path.relpath(path.resolve(), Path.cwd().resolve())).as_posix()",
     "        if not path.resolve().is_relative_to(Path.cwd().resolve()):\n"
     "            return path.as_posix()\n"
     "        return Path(os.path.relpath(path.resolve(), Path.cwd().resolve())).as_posix()"),
    # --- the exceptions spreadsheet
    ("exceptions sheet keeps only the first flag on each invoice", OUTPUTS,
     "            for finding in verdict.findings:", "            for finding in verdict.findings[:1]:"),
    ("files that could not be read left off the exceptions sheet", OUTPUTS,
     '        for item in unread:\n            writer.writerow([\n                safe_text(item.name), NOT_CHECKED,',
     '        for item in ():\n            writer.writerow([\n                safe_text(item.name), NOT_CHECKED,'),
    ("formula-like invoice text written into the CSVs as a formula", OUTPUTS,
     '_FORMULA_PREFIXES = ("=", "+", "-", "@", "\\t", "\\r")',
     '_FORMULA_PREFIXES = ("\\t", "\\r")'),
    ("amounts escaped like text, so the money columns cannot be added up", OUTPUTS,
     "_iso(inv.invoice_date), money_str(inv.total),\n                    finding.check,",
     "_iso(inv.invoice_date), safe_text(money_str(inv.total)),\n                    finding.check,"),
    ("a tax shortfall listed as overbilling on the exceptions sheet", OUTPUTS,
     '    kind = "overbilling" if finding.overcharge > 0 else "undercharge"',
     '    kind = "overbilling"'),
    ("the second copy's whole total left off the exceptions sheet", OUTPUTS,
     '    if finding.repeat_total is not None:\n        return money_str(finding.repeat_total), "sent twice"',
     '    if False:\n        return money_str(finding.repeat_total), "sent twice"'),
    # --- the import-template mapping
    ("import map: a source column that does not exist is accepted", MAP,
     "    if source not in available:", "    if False:"),
    ("import map: the same target column twice is accepted", MAP,
     "        if column.target in seen:", "        if False:"),
    ("import map: a column with neither a source nor a constant is accepted", MAP,
     "    if not has_from and not has_value:", "    if False:"),
    ("import map: a mistyped key in a column is ignored", MAP,
     "    unknown = sorted(set(entry) - _ENTRY_KEYS)", "    unknown = []"),
    ("import map: a source and a constant on the same column is accepted", MAP,
     "    if has_from and has_value:", "    if False:"),
    ("import map: a bad mapping does not stop the run", CLI,
     '            print(f"error: import map {args.import_map}: {problem}", file=err)\n            return EXIT_USAGE',
     '            print(f"error: import map {args.import_map}: {problem}", file=err)\n            mapping = None'),
    ("import map: columns left out of the mapped file are not named", CLI,
     "        if left_out:", "        if False:"),
    ("import map: a constant that looks like a formula is written as one", OUTPUTS,
     "            writer.writerow([safe_text(str(cell)) if isinstance(cell, str) else cell",
     "            writer.writerow([cell"),
    ("import map: the mapping is applied to import_ready.csv itself", CLI,
     '        write_mapped_import_csv(built, mapping, args.out / "import_ready_mapped.csv")',
     '        write_mapped_import_csv(built, mapping, args.out / "import_ready.csv")'),
    ("a history CSV with no invoice numbers is read as an empty history", VERIFY,
     '        if not reader.fieldnames or "InvoiceNumber" not in reader.fieldnames:',
     "        if False:"),
    # --- the review queue
    ("an approval clears a possible duplicate", REVIEW,
     "        blocked = [c for c in found if c in NEVER_APPROVABLE]", "        blocked = []"),
    ("approve records an approval for an invoice that may already have been paid", CLI,
     "    blocked = [c for c in checks if c in NEVER_APPROVABLE]", "    blocked = []"),
    ("a queue file edited to approve a duplicate is accepted", REVIEW,
     "    blocked = [c for c in held_for if c in NEVER_APPROVABLE]", "    blocked = []"),
    ("an approval covers a reason nobody approved", REVIEW,
     "        uncovered = [c for c in found if c not in approval.held_for]", "        uncovered = []"),
    ("an approval survives a change to the invoice's figures", REVIEW,
     '    payload = {k: v for k, v in invoice.to_dict().items() if k != "source_file"}',
     '    payload = {"invoice_number": invoice.invoice_number}'),
    ("the approval is tied to the file name as well as the invoice", REVIEW,
     '    payload = {k: v for k, v in invoice.to_dict().items() if k != "source_file"}',
     "    payload = invoice.to_dict()"),
    ("a stale approval is passed over without a word", REVIEW,
     "            stale = by_file.get(inv.source_file) or (by_number.get(key) if key else None)",
     "            stale = None"),
    ("an approval dropped by a rename and a change is passed over without a word", REVIEW,
     "            stale = by_file.get(inv.source_file) or (by_number.get(key) if key else None)",
     "            stale = by_file.get(inv.source_file)"),
    ("an invoice is APPROVED without an approval", VERIFY,
     "        return APPROVED if self.approval is not None else NEEDS_REVIEW", "        return APPROVED"),
    ("an approval with no name is recorded", CLI, "    if not who:", "    if False:"),
    ("approving an invoice that passed every check is allowed", CLI,
     "    if not findings:", "    if False:"),
    ("approving the same invoice twice is allowed", CLI,
     '    if entry.get("approval"):', "    if False:"),
    ("a second approval writes over the queue instead of joining it", REVIEW,
     "    existing = load_approvals(path) if path.is_file() else []", "    existing = []"),
    ("the reasons an invoice was held are not recorded on its approval", CLI,
     "        held_for=tuple(checks),", "        held_for=(),"),
    ("a trailing Z on a timestamp is not converted (raises on Python 3.10)", REVIEW,
     '    if s.endswith(("Z", "z")):', "    if False:"),
    ("results.json carries no content hash, so an approval has nothing to bind to", OUTPUTS,
     '                "content_hash": invoice_hash(v.invoice),', '                "content_hash": "",'),
    ("who approved an invoice is left out of the CSVs", OUTPUTS,
     '    return verdict.approval.approved_by if verdict.approval is not None else ""',
     '    return ""'),
    ("the reason given for an approval is not recorded", OUTPUTS,
     '    return verdict.approval.note if verdict.approval is not None else ""',
     '    return ""'),
    ("an approval that could not be used is not printed", CLI,
     "        if v.approval_note:", "        if False:"),
    ("the approved count is left out of the result line", OUTPUTS,
     '    middle = f"{approved} {APPROVED}, " if approved else ""', '    middle = ""'),
    ("a queue file that cannot be written crashes instead of saying so", CLI,
     "    except OSError as problem:\n        print(f\"error: could not write the review queue {args.approvals} \"",
     "    except ApprovalsError as problem:\n        print(f\"error: could not write the review queue {args.approvals} \""),
    ("a currency the AI read off the page written into the import CSV as a formula", OUTPUTS,
     "                _iso(inv.due_date), safe_text(inv.currency), n, safe_text(item.description),",
     "                _iso(inv.due_date), inv.currency, n, safe_text(item.description),"),
    ("an approver's note with a line break breaks the Markdown table", OUTPUTS,
     '    return _md(" ".join((text or "").split()))', "    return _md(text)"),
    ("overbilling a person approved into the import file is not reported", VERIFY,
     "    approved_overcharge = sum((v.overcharge for v in approved), ZERO)",
     "    approved_overcharge = ZERO"),
    # --- rules extractor
    ("banner line taken as the vendor", RULES,
     "    words = _WORDS.findall(clean(line).lower())",
     "    return False\n    words = _WORDS.findall(clean(line).lower())"),
    ("labelled vendor ignored", RULES,
     "def labelled_vendor(lines: list[str]) -> str | None:",
     "def labelled_vendor(lines: list[str]) -> str | None:\n    return None"),
    ("bare 'Date' label matches 'Date of service'", RULES,
     '    ("invoice_date", rf"date{_VALUE_FOLLOWS}"),', '    ("invoice_date", r"date"),'),
    ("unreadable text row dropped silently", RULES,
     '            inv.warnings.append(f"{where}: could not read an item row: {clean(line)!r}")',
     "            pass"),
    ("text totals classified before item rows", RULES,
     "        if m and not in_totals:", "        if m and not in_totals and not classify_total_line(line):"),
    ("row without cents merged into a description", RULES,
     "        elif MONEY_TOKEN.search(line) or _COLUMN_NUMBER.search(line):",
     "        elif MONEY_TOKEN.search(line):"),
    ("item row below the totals dropped silently", RULES,
     '            inv.warnings.append(f"{where}: an item row after the totals was not read: {clean(line)!r}")',
     "            pass"),
    ("unlabelled totals amount dropped silently", RULES,
     "def warn_unlabelled_amount(inv: Invoice, text: str, where: str) -> None:",
     "def warn_unlabelled_amount(inv: Invoice, text: str, where: str) -> None:\n    return"),
    ("html-table unreadable row dropped silently", RULES,
     '                    inv.warnings.append(f"{where}: could not read an item row: {cells}")',
     "                    pass"),
    ("html second items table ignored", RULES,
     "    for number, (_, header, columns, rows) in enumerate(tables, start=1):",
     "    for number, (_, header, columns, rows) in enumerate(tables[:1], start=1):"),
    # --- AI path
    ("AI refusal accepted", AI,
     '    if stop == "refusal":', '    if stop == "refusal-never":'),
    ("API key sent in body", AI,
     '        API_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",',
     '        API_URL, data=json.dumps({**body, "k": api_key}).encode("utf-8"), headers=headers, method="POST",'),
    ("read timeouts and dropped connections escape", AI,
     "        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:",
     "        except urllib.error.URLError as err:"),
    # --- the Python 3.10 floor
    # Each of these ships a name that does not exist on Ubuntu 22.04's Python. The
    # `from X import Y` spelling is here because the guard used to look only for the
    # `X.Y` one, so three of the seven features it advertised went straight through.
    ("3.11-only datetime.UTC imported into the package", MODEL,
     FUTURE, FUTURE + "from datetime import UTC\n"),
    ("3.11-only typing.Self imported into the package", MODEL,
     FUTURE, FUTURE + "from typing import Self\n"),
    ("3.11-only hashlib.file_digest imported into the package", MODEL,
     FUTURE, FUTURE + "from hashlib import file_digest\n"),
    ("3.12-only itertools.batched reached through the module", MODEL,
     FUTURE, FUTURE + "import itertools\n_BATCH = itertools.batched\n"),
    ("3.11-only datetime.UTC reached through a renamed module", MODEL,
     FUTURE, FUTURE + "import datetime as _dt\n_UTC = _dt.UTC\n"),
    # --- --out has to be a folder, and --today one spelling
    ("--out pointed at a file crashes instead of saying so", CLI,
     "    if args.out.exists() and not args.out.is_dir():", "    if False:"),
    ("an output folder that cannot be created crashes instead of saying so", CLI,
     '    except OSError as problem:\n        print(f"error: could not create the output folder {args.out} "',
     '    except ApprovalsError as problem:\n        print(f"error: could not create the output folder {args.out} "'),
    ("--today accepts spellings the Python 3.10 floor rejects", CLI,
     "    if not _YYYY_MM_DD.fullmatch(text):", "    if False:"),
    # --- the approver's name
    ("the approver's name lowercased in the review-queue lines", REVIEW,
     "    return line[:1].upper() + line[1:]", "    return line.capitalize()"),
]


def run_suite(root: Path) -> bool:
    proc = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=root,
                          capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        return check(Path(tmp) / "project")


def check(work: Path) -> int:
    # tools/ is copied too: tests/test_pdf.py checks pdf_samples/ against tools/make_pdf_samples.py.
    shutil.copytree(SRC, work, ignore=shutil.ignore_patterns("out", "__pycache__"))
    if not run_suite(work):
        print("baseline suite failed; fix it before mutating")
        return 2
    print("baseline: pass")
    survivors = 0
    for name, rel, old, new in MUTANTS:
        path = work / rel
        original = path.read_text(encoding="utf-8")
        if original.count(old) != 1:
            print(f"SKIP (pattern count {original.count(old)}): {name}")
            survivors += 1
            continue
        path.write_text(original.replace(old, new), encoding="utf-8")
        passed = run_suite(work)
        path.write_text(original, encoding="utf-8")
        print(f"{'SURVIVED' if passed else 'killed  '}: {name}")
        survivors += passed
    print(f"{len(MUTANTS) - survivors}/{len(MUTANTS)} mutants killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
