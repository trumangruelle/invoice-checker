"""Text from PDF invoices, through pypdf when it is installed.

pypdf is optional: everything else in the tool is standard library only. Without
it, each PDF is listed as NOT CHECKED with a one-line reason saying what to
install. A PDF is read only through its text layer (the words you can select);
a scanned PDF has none, and there is no OCR here, so it is listed as NOT CHECKED
too. So is a PDF that is damaged or password-protected.
"""

from __future__ import annotations

import io
import logging
import warnings

_NOT_LOADED = object()
# The pypdf module, None when it is not installed, or _NOT_LOADED until the first PDF.
# It is imported only when a PDF turns up, because importing it takes longer than
# checking a folder of text invoices.
pypdf = _NOT_LOADED


def load_pypdf():
    """pypdf if it is installed, else None."""
    global pypdf
    if pypdf is _NOT_LOADED:
        try:
            import pypdf as module
        except ImportError:  # optional dependency: PDFs are listed as NOT CHECKED without it
            module = None
        pypdf = module
    return pypdf


NEEDS_PYPDF = "install pypdf to read PDF invoices"
SCANNED = "scanned PDF (no text layer), not read: it needs OCR first, which this tool does not do"
NO_PAGES = "PDF, not read: it has no pages"
LOCKED = "PDF, not read: it is password-protected"
# An encrypted PDF (AES, even with no password to type) needs the cryptography package.
NEEDS_PACKAGE = "PDF, not read: pypdf needs another package to open it ({error})"
DAMAGED = "PDF, not read: it could not be opened ({error}); the file may be damaged"


class NotReadable(Exception):
    """A file that cannot be checked at all. The reason is one line, for the NOT CHECKED list."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def available() -> bool:
    return load_pypdf() is not None


def _page_text(page) -> str:
    # Layout mode keeps words where they sit on the page, so an items table comes out as
    # whitespace-aligned columns, which the text-columns reader understands.
    try:
        return page.extract_text(extraction_mode="layout")
    except Exception:  # noqa: BLE001 - some fonts defeat layout mode; plain mode still reads them
        return page.extract_text()


def _tidy(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def pdf_text(data: bytes) -> str:
    """The text layer of a PDF, page after page. Raises NotReadable with the reason when there is none."""
    pypdf = load_pypdf()
    if pypdf is None:
        raise NotReadable(NEEDS_PYPDF)
    logger = logging.getLogger("pypdf")
    level = logger.level
    logger.setLevel(logging.CRITICAL)   # a damaged file is reported once, as NOT CHECKED, not as log noise
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                reader = pypdf.PdfReader(io.BytesIO(data))
                pages = [_page_text(page) for page in reader.pages]
            except pypdf.errors.FileNotDecryptedError:
                raise NotReadable(LOCKED) from None
            except pypdf.errors.DependencyError as err:
                raise NotReadable(NEEDS_PACKAGE.format(error=" ".join(str(err).split()))) from None
            except Exception as err:  # noqa: BLE001 - any parser failure means the file was not read
                raise NotReadable(DAMAGED.format(error=type(err).__name__)) from None
    finally:
        logger.setLevel(level)
    if not pages:
        raise NotReadable(NO_PAGES)
    text = _tidy("\n".join(pages))
    if not text.strip():
        raise NotReadable(SCANNED)
    return text
