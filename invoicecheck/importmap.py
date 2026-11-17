"""Renaming and reordering the import file's columns to match an accounting package's template.

Every accounting package wants its own column names in its own order, and
usually a few fields the invoice does not carry at all (an expense account
code, a tax code, a currency). A mapping file says, once, how this tool's
generic `import_ready.csv` becomes that package's bill-import template.

**Why JSON and not a CSV mapping.** The mapping has to say three different
things per column: take this column, or write this constant, and (by leaving
a column out) drop it. A two-column CSV cannot tell "the column named Vendor"
apart from "the constant text Vendor", so it would need a third column of
kinds -- a small format of its own with no way to check it. JSON says it in
one word per entry, keeps the order visible on the page, and the standard
library reports the exact line and column of a typo. A mapping file is also
written once and then left alone, so it is not a spreadsheet job.

    {
      "template": "Example bill import",
      "columns": [
        {"target": "SupplierName",  "from": "Vendor"},
        {"target": "BillNumber",    "from": "InvoiceNumber"},
        {"target": "BillDate",      "from": "InvoiceDate"},
        {"target": "LineDescription", "from": "Description"},
        {"target": "LineQuantity",  "from": "Quantity"},
        {"target": "LineUnitPrice", "from": "UnitPrice"},
        {"target": "AccountCode",   "value": "6000"}
      ]
    }

docs/import_map_example.json is this mapping, and a test runs the CLI with it.
Column names differ from package to package; these are placeholders.

Anything the mapping does not name is left out of the mapped file, and the run
prints which columns those were. A `from` that names a column this tool does
not produce is an error, not a blank column: a silently empty "LineUnitPrice"
would import as zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_ENTRY_KEYS = {"target", "from", "value"}
_TOP_KEYS = {"template", "columns"}


class MappingError(Exception):
    """The mapping file could not be read, or asks for something impossible."""


@dataclass(frozen=True)
class Column:
    target: str                  # the column name the accounting package wants
    source: str | None = None    # a column of import_ready.csv
    value: str | None = None     # a constant written on every row


@dataclass(frozen=True)
class ImportMapping:
    columns: tuple[Column, ...]
    template: str = ""

    @property
    def targets(self) -> list[str]:
        return [c.target for c in self.columns]

    def sources(self) -> list[str]:
        return [c.source for c in self.columns if c.source is not None]

    def constants(self) -> list[str]:
        return [c.target for c in self.columns if c.value is not None]

    def left_out(self, available: Sequence[str]) -> list[str]:
        """Columns of import_ready.csv the mapping does not use, in their original order."""
        used = set(self.sources())
        return [name for name in available if name not in used]


def _entry_column(entry, where: str, available: Sequence[str]) -> Column:
    if not isinstance(entry, dict):
        raise MappingError(f"{where}: each column must be an object, for example "
                           f'{{"target": "*ContactName", "from": "Vendor"}}')
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise MappingError(f"{where}: unknown key(s) {', '.join(repr(k) for k in unknown)}; "
                           f"a column takes only 'target' and one of 'from' or 'value'")
    target = entry.get("target")
    if not isinstance(target, str) or not target.strip():
        raise MappingError(f"{where}: 'target' must be the column name to write, as a non-empty string")
    has_from, has_value = "from" in entry, "value" in entry
    if has_from and has_value:
        raise MappingError(f"{where} ({target!r}): give 'from' (a column to copy) or 'value' "
                           f"(a constant), not both")
    if not has_from and not has_value:
        raise MappingError(f"{where} ({target!r}): give 'from' (a column to copy) or 'value' "
                           f"(a constant to write on every row)")
    if has_value:
        value = entry["value"]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise MappingError(f"{where} ({target!r}): 'value' must be text or a number")
        return Column(target=target, value=str(value))
    source = entry["from"]
    if not isinstance(source, str):
        raise MappingError(f"{where} ({target!r}): 'from' must be the name of a column, as a string")
    if source not in available:
        raise MappingError(
            f"{where} ({target!r}): 'from' names {source!r}, which is not a column of "
            f"import_ready.csv. The columns are: {', '.join(available)}")
    return Column(target=target, source=source)


def parse_mapping(data, available: Sequence[str]) -> ImportMapping:
    """Check a parsed mapping against the columns this tool produces."""
    if not isinstance(data, dict):
        raise MappingError('it must be a JSON object with a "columns" list')
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise MappingError(f"unknown key(s) {', '.join(repr(k) for k in unknown)} at the top level; "
                           f"it takes 'columns' and an optional 'template' name")
    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        raise MappingError('"columns" must be a non-empty list of columns to write, in order')
    parsed = [_entry_column(entry, f"column {n}", available) for n, entry in enumerate(columns, start=1)]
    seen: dict[str, int] = {}
    for n, column in enumerate(parsed, start=1):
        if column.target in seen:
            raise MappingError(f"column {n}: {column.target!r} is already column {seen[column.target]}; "
                               f"each target column can be written only once")
        seen[column.target] = n
    template = data.get("template", "")
    if not isinstance(template, str):
        raise MappingError("'template' must be a name for the target format, as a string")
    return ImportMapping(columns=tuple(parsed), template=template)


def load_mapping(path: Path, available: Sequence[str]) -> ImportMapping:
    """Read and check a mapping file. Raises MappingError with the reason."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as err:
        raise MappingError(f"could not read it ({err.strerror or err})") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise MappingError(f"it is not valid JSON (line {err.lineno}, column {err.colno}: {err.msg})") from None
    return parse_mapping(data, available)


def map_row(mapping: ImportMapping, row: dict) -> list[str]:
    """One mapped output row. Every target column is filled from the row or from a constant."""
    return [row[c.source] if c.source is not None else c.value for c in mapping.columns]
