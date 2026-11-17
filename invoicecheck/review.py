"""The review queue: a person marks a held invoice approved, and the next run imports it.

An approval is a claim by a named person that they looked at one invoice and
it is fine. Three things make that claim worth something:

* it names **who** approved it and **when** (UTC), and records **why** the
  invoice was held, so the decision can be audited later;
* it is tied to the **content** of that invoice, not to its file name, so a
  file that changes after it was approved is held again rather than sliding
  through on an old approval;
* it can **never** clear a possible duplicate. Paying an invoice twice is the
  expensive failure this tool exists to prevent, and no approval overrides it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .textutil import alnum_upper
from .verify import LABELS, NEEDS_REVIEW, Verdict, vendor_key

# Checks an approval can never clear. An approval says "these figures are right";
# a duplicate finding says "you may have paid this already", which is a different
# question, and the answer is not on the invoice. See README, "The review queue".
NEVER_APPROVABLE = ("duplicate_number", "possible_duplicate", "already_seen")

FORMAT_VERSION = 1

_HEX = set("0123456789abcdef")


class ApprovalsError(Exception):
    """The review queue file could not be read, or an approval in it is not usable."""


@dataclass(frozen=True)
class Approval:
    """One recorded approval. Every field is written to the queue file."""
    file: str                       # the file name as it was in the run that held it
    content_hash: str               # sha256 of the extracted invoice (see invoice_hash)
    approved_by: str                # free text the user passed with --by
    approved_at: str                # ISO 8601 UTC, e.g. "2026-09-23T14:05:11+00:00"
    held_for: tuple[str, ...]       # the check codes this invoice was held for, at approval time
    held_reasons: tuple[str, ...]   # the same findings in plain English, for the audit trail
    vendor: str = ""
    invoice_number: str = ""
    total: str = ""
    note: str = ""                  # free text from --note

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "content_hash": self.content_hash,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "held_for": list(self.held_for),
            "held_reasons": list(self.held_reasons),
            "vendor": self.vendor,
            "invoice_number": self.invoice_number,
            "total": self.total,
            "note": self.note,
        }

    @property
    def approved_on(self) -> str:
        """Just the date, for a report line ("approved by Dana Fell on 2026-09-23")."""
        return parse_timestamp(self.approved_at).date().isoformat()


# --------------------------------------------------------------------------
# What "the same invoice" means
# --------------------------------------------------------------------------

def invoice_hash(invoice) -> str:
    """A fingerprint of everything the extractor read, apart from the file name.

    The approval is about the numbers a person checked, so the hash covers the
    vendor, the invoice number, the dates, every line item, the subtotal, the
    tax, the total, the extractor's own warnings, and which extractor and
    layout produced them. Change any of those and the approval stops applying.

    The file name is left out on purpose: the same invoice saved under another
    name is still the same invoice. Renaming it therefore does not void the
    approval, and does not create a second one either -- and because an
    approval can never clear a duplicate finding, a second copy of an approved
    invoice is still held.
    """
    payload = {k: v for k, v in invoice.to_dict().items() if k != "source_file"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def now_stamp() -> str:
    """The current time in UTC, to the second, as ISO 8601 with a "+00:00" offset.

    Not "Z": datetime.fromisoformat() cannot read a trailing "Z" before Python
    3.11, and Ubuntu 22.04 LTS ships 3.10.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_utc_text(text: str) -> str:
    """The same ISO 8601 timestamp with a trailing "Z" written as "+00:00".

    datetime.fromisoformat() raises on a trailing "Z" before Python 3.11, and
    this project's floor is 3.10 (Ubuntu 22.04 LTS, where clients deploy). A
    queue file written by another tool, or edited by hand, often carries one,
    so the "Z" is converted before it reaches fromisoformat -- on every
    version, so that nothing here depends on which interpreter wrote it.

    This is a separate function because a newer interpreter accepts the "Z"
    either way, so only checking the text it produces can show the conversion
    is still happening.
    """
    s = (text or "").strip()
    if s.endswith(("Z", "z")):
        return s[:-1] + "+00:00"
    return s


def parse_timestamp(text: str) -> datetime:
    """Read an ISO 8601 timestamp, including one that ends in "Z"."""
    return datetime.fromisoformat(iso_utc_text(text))


# --------------------------------------------------------------------------
# Reading and writing the queue file
# --------------------------------------------------------------------------

def _require_str(entry: dict, key: str, where: str, *, allow_empty: bool = False) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise ApprovalsError(f"{where}: {key!r} must be a non-empty string")
    return value


def _require_str_list(entry: dict, key: str, where: str) -> tuple[str, ...]:
    value = entry.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ApprovalsError(f"{where}: {key!r} must be a list of strings")
    return tuple(value)


def _approval_from_dict(entry, where: str) -> Approval:
    if not isinstance(entry, dict):
        raise ApprovalsError(f"{where}: each approval must be an object")
    content_hash = _require_str(entry, "content_hash", where).lower()
    if len(content_hash) != 64 or set(content_hash) - _HEX:
        raise ApprovalsError(f"{where}: 'content_hash' must be 64 hexadecimal characters")
    approved_at = _require_str(entry, "approved_at", where)
    try:
        parse_timestamp(approved_at)
    except ValueError as err:
        raise ApprovalsError(f"{where}: 'approved_at' is not a date and time I can read "
                             f"({approved_at!r}: {err})") from None
    held_for = _require_str_list(entry, "held_for", where)
    # A hand-edited queue cannot smuggle a duplicate past the check either: the
    # run stops rather than quietly applying an approval that must not exist.
    blocked = [c for c in held_for if c in NEVER_APPROVABLE]
    if blocked:
        raise ApprovalsError(
            f"{where}: this approval lists {', '.join(sorted(set(blocked)))}, which an approval can "
            f"never clear; an invoice that may already have been paid is not released by approving it")
    return Approval(
        file=_require_str(entry, "file", where),
        content_hash=content_hash,
        approved_by=_require_str(entry, "approved_by", where),
        approved_at=approved_at,
        held_for=held_for,
        held_reasons=_require_str_list(entry, "held_reasons", where),
        vendor=str(entry.get("vendor") or ""),
        invoice_number=str(entry.get("invoice_number") or ""),
        total=str(entry.get("total") or ""),
        note=str(entry.get("note") or ""),
    )


def load_approvals(path: Path) -> list[Approval]:
    """Every approval in the queue file. Raises ApprovalsError with the reason."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as err:
        raise ApprovalsError(f"could not read it ({err.strerror or err})") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ApprovalsError(f"it is not valid JSON (line {err.lineno}, column {err.colno}: {err.msg})") from None
    if not isinstance(data, dict) or not isinstance(data.get("approvals"), list):
        raise ApprovalsError('it must be a JSON object with an "approvals" list')
    return [_approval_from_dict(entry, f"approval {n}")
            for n, entry in enumerate(data["approvals"], start=1)]


def save_approvals(path: Path, approvals: Sequence[Approval]) -> None:
    """Write the queue file, replacing it in one step so a crash cannot truncate it."""
    data = {"version": FORMAT_VERSION, "approvals": [a.to_dict() for a in approvals]}
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def add_approval(path: Path, approval: Approval) -> list[Approval]:
    """Append one approval to the queue file (creating it), and return the whole queue.

    The existing file is read and validated first, so a queue that cannot be
    read is never overwritten with a new one.
    """
    existing = load_approvals(path) if path.is_file() else []
    approvals = list(existing) + [approval]
    save_approvals(path, approvals)
    return approvals


# --------------------------------------------------------------------------
# Applying the queue to a run
# --------------------------------------------------------------------------

def _labels(checks: Iterable[str]) -> str:
    return ", ".join(dict.fromkeys(LABELS.get(c, c) for c in checks))


def _by_line(approval: Approval) -> str:
    return f"approved by {approval.approved_by} on {approval.approved_on}"


def _opening(approval: Approval) -> str:
    """The same line, starting a sentence: the first letter changes and nothing else.

    str.capitalize() was here and lowercased the rest of the string, so
    "approved by Dana Fell" was printed as "Approved by dana fell". This line's
    whole job is to name the person who approved an invoice, in the terminal,
    in results.json and in both reports, so it has to spell their name the way
    they gave it.
    """
    line = _by_line(approval)
    return line[:1].upper() + line[1:]


def _latest(approvals: Sequence[Approval]) -> Approval:
    """The most recent of several approvals, so re-approving replaces the older decision."""
    return max(approvals, key=lambda a: (parse_timestamp(a.approved_at), a.approved_by))


def _vendor_number(vendor: str | None, number: str | None) -> tuple[str, str] | None:
    """Vendor and invoice number, matched the way the duplicate checks match them.

    Used only to find an approval that no longer applies, so that a file which was
    both renamed and changed still gets a note instead of nothing.
    """
    key = alnum_upper(number)
    return (vendor_key(vendor), key) if key else None


def apply_approvals(verdicts: Sequence[Verdict], approvals: Sequence[Approval]) -> None:
    """Mark each held verdict APPROVED where a usable approval covers it.

    A verdict that is not covered keeps its NEEDS REVIEW status and, when an
    approval was on file but could not be used, carries a note saying why. The
    note is printed and written to every report: an approval that stopped
    working must never fail silently.
    """
    by_hash: dict[str, list[Approval]] = {}
    by_file: dict[str, list[Approval]] = {}
    by_number: dict[tuple[str, str], list[Approval]] = {}
    for approval in approvals:
        by_hash.setdefault(approval.content_hash, []).append(approval)
        by_file.setdefault(approval.file, []).append(approval)
        key = _vendor_number(approval.vendor, approval.invoice_number)
        if key is not None:
            by_number.setdefault(key, []).append(approval)

    for verdict in verdicts:
        verdict.approval = None
        verdict.approval_note = None
        if verdict.status != NEEDS_REVIEW:
            continue
        digest = invoice_hash(verdict.invoice)
        matching = by_hash.get(digest)
        if not matching:
            # The hash does not match, so no approval applies. Say so anyway if one was
            # written for this file, or for this vendor and invoice number -- a file that
            # was renamed *and* changed would otherwise lose its approval without a word.
            inv = verdict.invoice
            key = _vendor_number(inv.vendor, inv.invoice_number)
            stale = by_file.get(inv.source_file) or (by_number.get(key) if key else None)
            if stale:
                approval = _latest(stale)
                verdict.approval_note = (
                    f"An approval for {approval.file} ({_by_line(approval)}) was not used: the "
                    f"invoice has changed since it was approved, so it is held again.")
            continue

        approval = _latest(matching)
        found = list(dict.fromkeys(f.check for f in verdict.findings))
        blocked = [c for c in found if c in NEVER_APPROVABLE]
        if blocked:
            verdict.approval_note = (
                f"{_opening(approval)}, but still held: {_labels(blocked)}. An approval "
                f"never clears a possible duplicate, because paying an invoice twice is the mistake "
                f"this check exists to stop.")
            continue
        uncovered = [c for c in found if c not in approval.held_for]
        if uncovered:
            verdict.approval_note = (
                f"{_opening(approval)} for {_labels(approval.held_for)}, but this run "
                f"also found {_labels(uncovered)}, which that approval does not cover.")
            continue
        verdict.approval = approval
