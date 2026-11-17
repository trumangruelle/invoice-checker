"""Scale check: a large synthetic batch with a known answer for every file, checked and timed.

    python tools/benchmark.py                   # 1,000 invoices, seed 1
    python tools/benchmark.py --count 5000 --seed 7

The batch is written to a temporary folder (see tests/synthetic.py for what is
planted), checked through the same main() the command line uses, and every
verdict is compared with the answer key. The check is timed twice: once over
the freshly written files, and again over the same files. Each time covers
reading, checking and writing all five outputs; generating the batch is not
timed. On Windows the first pass can be many times slower, because opening a
file that was just written is slow there; the profile of that pass is almost
all file opens, not checking.
"""
import argparse
import io
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from invoicecheck.cli import main as invoicecheck  # noqa: E402
from invoicecheck.textutil import fmt_money  # noqa: E402
from tests.synthetic import MISTAKES, TODAY, make_batch, score  # noqa: E402


def timed_check(batch: Path, out: Path) -> float:
    started = time.perf_counter()
    code = invoicecheck(["run", "--input", str(batch), "--out", str(out), "--today", TODAY.isoformat()],
                        env={}, stdout=io.StringIO(), stderr=sys.stderr)
    seconds = time.perf_counter() - started
    if code != 0:
        raise SystemExit(f"invoicecheck exited with {code}")
    return seconds


def run(count: int, seed: int) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        batch, out = Path(tmp) / "batch", Path(tmp) / "out"
        key = make_batch(batch, count, seed)
        first = timed_check(batch, out)
        repeat = timed_check(batch, out)
        s = score(key, json.loads((out / "results.json").read_text(encoding="utf-8")))

    planted = s.invoices - s.should_pass
    print(f"Synthetic batch: {s.invoices:,} invoices (seed {seed}), 4 layouts, {s.files:,} files")
    print(f"  {planted:,} invoices have one planted mistake ({len(MISTAKES)} kinds); "
          f"each one sent twice adds a reminder copy")
    print(f"Should pass:    {s.should_pass:,} files -> {s.passed:,} passed, {s.false_alarms} false alarms")
    print(f"Should be held: {s.should_hold:,} files -> {s.held_right:,} held for the right reasons, "
          f"{s.missed} missed, {s.wrong_reason} held for a different reason")
    print(f"Overbilling (arithmetic errors) planted {fmt_money(s.overbilling_planted)}, "
          f"found {fmt_money(s.overbilling_found)}")
    print(f"Sent twice: {s.sent_twice_planted:,} invoices ({fmt_money(s.sent_twice_planted_total)}) planted, "
          f"{s.sent_twice_found:,} ({fmt_money(s.sent_twice_found_total)}) held so they are not paid twice")
    print(f"Checked in {first:.2f} s over the freshly written files, {repeat:.2f} s on a repeat run "
          f"({repeat / s.files * 1000:.2f} s per 1,000 files)")
    for problem in s.problems[:20]:
        print(f"  {problem}")
    return 0 if s.perfect else 1


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=1000, help="invoices to generate (default 1000)")
    parser.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    args = parser.parse_args()
    return run(args.count, args.seed)


if __name__ == "__main__":
    raise SystemExit(cli())
