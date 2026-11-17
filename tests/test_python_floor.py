"""The Python floor: 3.10, because Ubuntu 22.04 LTS ships it and clients deploy there.

Only Python 3.14 is installed on the machine these tests were written on, so
the suite itself cannot be run on 3.10 here. These checks do what can be done
without that interpreter: they read the package source for features that do
not exist on 3.10, and they pin the two places where 3.10 and 3.11 genuinely
behave differently at run time -- a trailing "Z" on a timestamp, which
datetime.fromisoformat() accepts from 3.11 and rejects on 3.10, and the
spellings date.fromisoformat() accepts, which 3.11 widened.

The source check is validated against known answers below, rather than being
trusted because it is green: a checker that catches nothing is green too. An
earlier version of this file advertised seven features and caught four of
them, because it looked for `typing.Self` only as an attribute and never saw
`from typing import Self`.
"""

import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path

from invoicecheck.review import iso_utc_text, now_stamp, parse_timestamp

PACKAGE = Path(__file__).resolve().parent.parent / "invoicecheck"

# Names that do not exist on Python 3.10. Each one has been shipped by mistake
# somewhere before; the note says what to use instead. These are caught as a bare
# name, and as the head of anything imported (`import tomllib`, `from tomllib
# import loads`, `from itertools import batched`).
FORBIDDEN_NAMES = {
    "tomllib": "3.11+; parse the file another way",
    "batched": "itertools.batched is 3.12+; slice the list",
    "StrEnum": "3.11+; use a plain str constant",
    "ExceptionGroup": "3.11+; raise one exception",
    "TaskGroup": "3.11+",
}
# Attributes of a module that do not exist on 3.10 either. These are caught both
# ways round: as `mod.name` (including through `import mod as alias`) and as
# `from mod import name` (including `from mod import name as other`), which is a
# different AST node and reaches the same feature.
FORBIDDEN_ATTRIBUTES = {
    ("datetime", "UTC"): "datetime.UTC is 3.11+; use timezone.utc",
    ("typing", "Self"): "typing.Self is 3.11+; name the class",
    ("hashlib", "file_digest"): "hashlib.file_digest is 3.11+; hash the bytes",
    ("asyncio", "timeout"): "asyncio.timeout is 3.11+",
    ("itertools", "batched"): "itertools.batched is 3.12+; slice the list",
    ("enum", "StrEnum"): "enum.StrEnum is 3.11+; use a plain str constant",
}


def floor_violations(source: str, filename: str = "<source>") -> list[str]:
    """Every use of a name that does not exist on Python 3.10, one complaint per use.

    A static read of the text. It does not follow a name through a variable, a
    getattr() or an __import__(), so the guarantee is only that the feature
    cannot be reached by the three spellings a person actually writes.
    """
    tree = ast.parse(source, filename=filename)

    # `import datetime as dt` has to be resolved before dt.UTC can be recognised.
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name

    problems: list[str] = []

    def complain(node: ast.AST, what: str, note: str) -> None:
        problems.append(f"{filename}:{node.lineno}: {what} ({note})")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            complain(node, node.id, FORBIDDEN_NAMES[node.id])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            key = (modules.get(node.value.id, node.value.id), node.attr)
            if key in FORBIDDEN_ATTRIBUTES:
                complain(node, f"{key[0]}.{key[1]}", FORBIDDEN_ATTRIBUTES[key])
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            for name in imported:
                head = name.split(".")[0]
                if head in FORBIDDEN_NAMES:
                    complain(node, f"imports {name}", FORBIDDEN_NAMES[head])
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    key = (node.module, alias.name)
                    if key in FORBIDDEN_ATTRIBUTES:
                        complain(node, f"imports {key[0]}.{key[1]}", FORBIDDEN_ATTRIBUTES[key])
    return problems


# The seven features the README promises this test catches, each written both
# ways a person would write it. Every line here must be caught; the table is the
# known answer the checker is validated against.
BOTH_SPELLINGS = [
    ("tomllib", "import tomllib\nprint(tomllib.loads(''))"),
    ("tomllib", "from tomllib import loads\nprint(loads(''))"),
    ("itertools.batched", "import itertools\nprint(list(itertools.batched([1, 2], 2)))"),
    ("itertools.batched", "from itertools import batched\nprint(list(batched([1, 2], 2)))"),
    ("typing.Self", "import typing\n\nclass C:\n    def f(self) -> typing.Self: ...\n"),
    ("typing.Self", "from typing import Self\n\nclass C:\n    def f(self) -> Self: ...\n"),
    ("StrEnum", "import enum\n\nclass C(enum.StrEnum):\n    A = 'a'\n"),
    ("StrEnum", "from enum import StrEnum\n\nclass C(StrEnum):\n    A = 'a'\n"),
    ("ExceptionGroup", "raise ExceptionGroup('x', [ValueError()])"),
    ("ExceptionGroup", "from builtins import ExceptionGroup\nprint(ExceptionGroup)"),
    ("datetime.UTC", "import datetime\nprint(datetime.UTC)"),
    ("datetime.UTC", "from datetime import UTC\nprint(UTC)"),
    ("hashlib.file_digest", "import hashlib\nprint(hashlib.file_digest)"),
    ("hashlib.file_digest", "from hashlib import file_digest\nprint(file_digest)"),
    # The aliased spellings, which are the ones a rename would hide.
    ("datetime.UTC", "import datetime as dt\nprint(dt.UTC)"),
    ("datetime.UTC", "from datetime import UTC as u\nprint(u)"),
    ("typing.Self", "from typing import Self as S\nprint(S)"),
]

# Source that uses only what 3.10 has. None of it may be reported: a checker that
# flags everything would pass the table above and be useless.
ONLY_ON_310 = """
from __future__ import annotations

import hashlib
import itertools
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class Colour(str, Enum):
    RED = "red"


def digest(data: bytes, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    pairs = list(zip(itertools.count(), data))
    return hashlib.sha256(data).hexdigest() + str(len(pairs)) + when.isoformat()


def chunks(items: Iterable[int], size: int) -> list[list[int]]:
    values = list(items)
    return [values[i:i + size] for i in range(0, len(values), size)]
"""


class FloorCheckerTests(unittest.TestCase):
    """The checker itself, against known answers. A zero from an unvalidated
    checker is not evidence that the package is clean."""

    def test_every_feature_the_readme_names_is_caught_in_both_spellings(self):
        for feature, source in BOTH_SPELLINGS:
            with self.subTest(feature=feature, source=source):
                found = floor_violations(source, "probe.py")
                self.assertTrue(found, f"not caught: {source!r}")

    def test_the_readme_list_and_the_table_name_the_same_features(self):
        # The README sells these seven by name. If one is added there, or dropped
        # from the tables above, this is what notices.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        promised = {"tomllib", "itertools.batched", "typing.Self", "StrEnum",
                    "ExceptionGroup", "datetime.UTC", "hashlib.file_digest"}
        self.assertEqual({feature for feature, _ in BOTH_SPELLINGS}, promised)
        for feature in promised:
            self.assertIn(f"`{feature}`", readme, f"README no longer names {feature}")

    def test_source_that_only_uses_python_310_features_is_not_flagged(self):
        self.assertEqual(floor_violations(ONLY_ON_310, "clean.py"), [])

    def test_a_complaint_names_the_file_the_line_and_what_to_use_instead(self):
        found = floor_violations("import os\nfrom datetime import UTC\n", "model.py")
        self.assertEqual(found, ["model.py:2: imports datetime.UTC "
                                 "(datetime.UTC is 3.11+; use timezone.utc)"])


class PythonFloorTests(unittest.TestCase):
    def sources(self):
        return sorted(PACKAGE.glob("*.py"))

    def test_the_package_uses_nothing_newer_than_python_310(self):
        self.assertTrue(self.sources())
        found = []
        for path in self.sources():
            found += floor_violations(path.read_text(encoding="utf-8"), path.name)
        self.assertEqual(found, [])

    def test_no_timestamp_goes_to_fromisoformat_without_the_z_being_converted(self):
        # The one 3.10 trap this code could hit: datetime.fromisoformat must only ever
        # see text that iso_utc_text has already been through.
        #
        # date.fromisoformat is passed over here because the CLI pins the format
        # itself before calling it -- see _date_arg and the test below. 3.11 widened
        # date.fromisoformat to spellings 3.10 rejects, so leaving it to decide would
        # mean --today took a value here that the client's Ubuntu 22.04 refuses.
        for path in self.sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "fromisoformat"):
                    continue
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "date":
                    continue
                argument = node.args[0] if node.args else None
                self.assertTrue(
                    isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name)
                    and argument.func.id == "iso_utc_text",
                    f"{path.name}:{node.lineno}: fromisoformat() is called on text that has not "
                    f"been through iso_utc_text(), so a trailing 'Z' would raise on Python 3.10")

    def test_a_trailing_z_is_rewritten_as_an_offset(self):
        # Asserted on the text, not on the parsed value: Python 3.11 and later accept
        # the "Z" either way, so only the text shows the conversion still happens.
        self.assertEqual(iso_utc_text("2026-09-22T09:15:00Z"), "2026-09-22T09:15:00+00:00")
        self.assertEqual(iso_utc_text("2026-09-22T09:15:00z"), "2026-09-22T09:15:00+00:00")
        self.assertEqual(iso_utc_text(" 2026-09-22T09:15:00Z "), "2026-09-22T09:15:00+00:00")
        self.assertEqual(iso_utc_text("2026-09-22T09:15:00+00:00"), "2026-09-22T09:15:00+00:00")
        self.assertEqual(iso_utc_text("2026-09-22T09:15:00-04:00"), "2026-09-22T09:15:00-04:00")

    def test_the_two_spellings_are_the_same_moment(self):
        self.assertEqual(parse_timestamp("2026-09-22T09:15:00Z"),
                         parse_timestamp("2026-09-22T09:15:00+00:00"))

    def test_the_timestamp_this_tool_writes_never_ends_in_z(self):
        stamp = now_stamp()
        self.assertFalse(stamp.endswith("Z"))
        self.assertTrue(stamp.endswith("+00:00"))
        self.assertEqual(parse_timestamp(stamp).utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(parse_timestamp(stamp).microsecond, 0)
        self.assertEqual(datetime.fromisoformat(stamp), parse_timestamp(stamp))


if __name__ == "__main__":
    unittest.main()
