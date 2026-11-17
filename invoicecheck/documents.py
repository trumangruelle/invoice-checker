"""Loading invoice files from disk into a uniform Document, and listing what cannot be read."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .htmldoc import Node, parse_html, to_text
from .pdftext import NotReadable, pdf_text

SUPPORTED_SUFFIXES = {".txt": "text", ".text": "text", ".html": "html", ".htm": "html"}
# PDFs are read through their text layer when pypdf is installed; see pdftext.py.
PDF_SUFFIX = ".pdf"

# Operating-system clutter that is never an invoice. These, Office lock files ("~$...")
# and anything hidden (a name starting with ".") are skipped: not read, and left out of
# the NOT CHECKED list. Every other file in the folder is either read or listed with a reason.
_SYSTEM_FILES = {"thumbs.db", "desktop.ini"}

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".heic", ".webp"}

NOT_UTF8_WARNING = ("the file is not valid UTF-8 text; {count} unreadable character(s) were "
                    "replaced, so a name or description may be wrong")


@dataclass
class Document:
    name: str          # path relative to the input folder, forward slashes
    kind: str          # "text" or "html" (a PDF's text layer is "text")
    raw: str           # the file exactly as read (for a PDF, its text layer)
    text: str          # a text rendering (the raw text for .txt files)
    dom: Node | None = None
    warnings: list[str] = field(default_factory=list)   # problems reading the file itself


@dataclass(frozen=True)
class Unread:
    """A file in the input folder that was not checked, and why."""
    name: str          # path relative to the input folder, forward slashes
    reason: str


def unread_reason(path: Path) -> str:
    if path.suffix.lower() in _IMAGE_SUFFIXES:
        return "image, not read: it needs OCR first"
    return "not read: only .txt, .html and .pdf invoices are checked"


def is_invoice_file(path: Path) -> bool:
    """A file the tool tries to read (a PDF may still turn out to be unreadable)."""
    suffix = path.suffix.lower()
    return suffix in SUPPORTED_SUFFIXES or suffix == PDF_SUFFIX


def document_from_string(name: str, raw: str, kind: str | None = None) -> Document:
    if kind is None:
        kind = SUPPORTED_SUFFIXES.get(Path(name).suffix.lower(), "text")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if kind == "html":
        dom = parse_html(raw)
        return Document(name=name, kind="html", raw=raw, text=to_text(dom), dom=dom)
    return Document(name=name, kind="text", raw=raw, text=raw)


def decode(data: bytes) -> tuple[str, list[str]]:
    """UTF-8 text (a byte-order mark is fine). Undecodable bytes are replaced and reported."""
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        text = data.decode("utf-8-sig", errors="replace")
        return text, [NOT_UTF8_WARNING.format(count=text.count("\ufffd"))]


def load_document(path: Path, base: Path | None = None) -> Document:
    """Read one invoice file. Raises NotReadable (with the reason) for a PDF with no usable text."""
    if base is not None:
        name = path.relative_to(base).as_posix()
    else:
        name = path.name
    if path.suffix.lower() == PDF_SUFFIX:
        # The text layer, checked exactly like a .txt invoice.
        return document_from_string(name, pdf_text(path.read_bytes()), "text")
    raw, warnings = decode(path.read_bytes())
    doc = document_from_string(name, raw, SUPPORTED_SUFFIXES.get(path.suffix.lower()))
    doc.warnings.extend(warnings)
    return doc


def _ignored(relative: Path) -> bool:
    return (any(part.startswith(".") for part in relative.parts)
            or relative.name.lower() in _SYSTEM_FILES
            or relative.name.startswith("~$"))


def scan_folder(folder: Path) -> tuple[list[Path], list[Unread]]:
    """Invoice files under a folder (recursively) in a stable order, and every other file with a reason."""
    readable: list[Path] = []
    unread: list[Unread] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if _ignored(relative):
            # Checked first, so a Mac "._invoice.pdf" copy (resource data, not a PDF) is not
            # reported as a damaged invoice.
            continue
        if is_invoice_file(path):
            readable.append(path)
        else:
            unread.append(Unread(relative.as_posix(), unread_reason(path)))
    readable.sort(key=lambda p: p.relative_to(folder).as_posix().lower())
    unread.sort(key=lambda u: u.name.lower())
    return readable, unread


def load_folder(folder: Path) -> tuple[list[Document], list[Unread]]:
    """Every invoice under a folder, read; and every file that was not, with its reason."""
    paths, unread = scan_folder(folder)
    docs: list[Document] = []
    for path in paths:
        try:
            docs.append(load_document(path, folder))
        except NotReadable as err:
            unread.append(Unread(path.relative_to(folder).as_posix(), err.reason))
    unread.sort(key=lambda u: u.name.lower())
    return docs, unread
