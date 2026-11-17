"""Shared test helpers: paths, an invoice builder and a fake urllib opener."""

from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from datetime import date
from decimal import Decimal
from email.message import Message
from pathlib import Path

from invoicecheck.cli import main
from invoicecheck.documents import load_document
from invoicecheck.model import Invoice, LineItem

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 9, 21)


def sample(name: str):
    return load_document(SAMPLES / name, SAMPLES)


def fixture_doc(name: str):
    return load_document(FIXTURES / name, FIXTURES)


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def D(value) -> Decimal:
    return Decimal(str(value))


def make_invoice(**overrides) -> Invoice:
    """A consistent, passing invoice; override fields to break one thing at a time."""
    items = overrides.pop("line_items", None)
    if items is None:
        items = [
            LineItem("Widget", D("2"), D("10.00"), D("20.00")),
            LineItem("Gadget", D("3"), D("5.50"), D("16.50")),
        ]
    fields = dict(
        source_file="test.txt",
        layout="text-columns",
        vendor="Test Vendor Co.",
        invoice_number="T-100",
        invoice_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        line_items=items,
        subtotal=D("36.50"),
        tax=D("2.92"),
        tax_rate=D("8"),
        total=D("39.42"),
    )
    fields.update(overrides)
    return Invoice(**fields)


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, body: dict | None = None, retry_after: str | None = None):
    headers = Message()
    if retry_after is not None:
        headers["retry-after"] = retry_after
    data = json.dumps(body or {}).encode("utf-8")
    return urllib.error.HTTPError("https://api.anthropic.com/v1/messages", code, "error",
                                  headers, io.BytesIO(data))


class FakeOpener:
    """Stands in for urllib.request.urlopen. Each call pops the next scripted outcome:
    a dict (returned as the JSON body) or an exception (raised)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("FakeOpener called more times than scripted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode("utf-8"))


class CliCase(unittest.TestCase):
    """A scratch input and output folder, with main() driven exactly as the terminal drives it.

    Every test that uses this goes through the real command line: argument
    parsing, the files that are written and the text that is printed. A helper
    that main() does not call cannot make one of these tests pass.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.input = self.work / "invoices"
        self.input.mkdir()
        self.out = self.work / "out"
        self.queue = self.work / "review_queue.json"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv: str, env=None, opener=None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main([str(a) for a in argv], env={} if env is None else env,
                    opener=opener, sleep=lambda _seconds: None, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_cli(self, *extra: str, today: str = "2026-09-21", env=None) -> tuple[int, str, str]:
        return self.cli("run", "--input", self.input, "--out", self.out, "--today", today,
                        *extra, env=env)

    def copy_samples(self, *names: str) -> None:
        for name in names:
            shutil.copy(SAMPLES / name, self.input / name)

    def write_invoice(self, name: str, text: str) -> Path:
        path = self.input / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, name: str, text: str) -> Path:
        path = self.work / name
        path.write_text(text, encoding="utf-8")
        return path

    def results(self) -> dict:
        return json.loads((self.out / "results.json").read_text(encoding="utf-8"))

    def statuses(self) -> dict:
        return {i["invoice"]["source_file"]: i["status"] for i in self.results()["invoices"]}

    def read_csv(self, name: str) -> list[dict]:
        with (self.out / name).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def read_rows(self, name: str) -> list[list[str]]:
        with (self.out / name).open(newline="", encoding="utf-8") as handle:
            return list(csv.reader(handle))
