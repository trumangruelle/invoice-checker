"""Write the two sample PDF invoices in pdf_samples/, using nothing but the standard library.

    python tools/make_pdf_samples.py

Each file is a minimal, valid PDF 1.4 document: one page, the built-in
Courier font, and a content stream that places each piece of text (a label,
a description, a quantity, an amount) at its own spot on the page, the way
invoicing software does. That gives it a real text layer, with no spaces
between the columns: the reader has to work the columns out from where the
text sits. The output is byte-for-byte the same on every run (no dates or
IDs are embedded), so a test can check that pdf_samples/ is what this
script writes.

    python tools/make_pdf_samples.py --scan FILE

writes a stand-in for a scanned invoice instead: one page holding an image
and no text at all. tests/test_pdf.py uses pdf_bytes() the same way, to
build scans and other PDFs at test time.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "pdf_samples"

PAGE_WIDTH, PAGE_HEIGHT = 612, 792     # US Letter, in points
FONT_SIZE, LEADING = 9, 12
LEFT, TOP = 54, 740
CHAR_WIDTH = FONT_SIZE * 0.6           # every Courier character is 600/1000 of the font size wide
_CELL = re.compile(r"\S+(?: \S+)*")    # text up to a gap of two or more spaces


def _pdf_string(text: str) -> bytes:
    """A PDF literal string: ( ) and backslash escaped. Only printable ASCII is allowed."""
    if any(not (32 <= ord(ch) < 127) for ch in text):
        raise ValueError(f"only printable ASCII is supported: {text!r}")
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("ascii") + b")"


def _points(value: float) -> str:
    return format(round(value, 2), "g")


def text_stream(lines: list[str]) -> bytes:
    """Content that draws the lines top to bottom, each cell placed where its column starts.

    A line is split at every gap of two or more spaces, and each piece is drawn on
    its own at its column's position, so the page has no space characters between
    columns (as in a PDF from invoicing software).
    """
    ops = [b"BT", f"/F1 {FONT_SIZE} Tf".encode("ascii")]
    for row, line in enumerate(lines):
        y = TOP - row * LEADING
        for cell in _CELL.finditer(line):
            x = LEFT + cell.start() * CHAR_WIDTH
            ops.append(f"1 0 0 1 {_points(x)} {_points(y)} Tm".encode("ascii"))
            ops.append(_pdf_string(cell.group()) + b" Tj")
    ops.append(b"ET")
    return b"\n".join(ops) + b"\n"


def image_stream() -> bytes:
    """Content that paints one full-page image and no text: what a scanner produces."""
    return f"q {PAGE_WIDTH} 0 0 {PAGE_HEIGHT} 0 0 cm /Im1 Do Q\n".encode("ascii")


def pdf_bytes(lines: list[str] | None = None, *, scanned: bool = False) -> bytes:
    """A one-page PDF. With lines, the page has a text layer; with scanned=True, only an image."""
    if (lines is None) == (not scanned):
        raise ValueError("give either lines or scanned=True")
    # Object 5 is the page's one resource: the font for a text page, the image for a scan.
    if scanned:
        # An 8 x 8 grey image, uncompressed: a stand-in for a scanned page.
        pixels = bytes((200 + (x * y) % 40) for y in range(8) for x in range(8))
        resources = b"<< /XObject << /Im1 5 0 R >> >>"
        content = image_stream()
        resource = (b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray "
                    b"/BitsPerComponent 8 /Length %d >>\nstream\n%s\nendstream" % (len(pixels), pixels))
    else:
        resources = b"<< /Font << /F1 5 0 R >> >>"
        content = text_stream(lines)
        resource = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents 4 0 R >>"
        % (PAGE_WIDTH, PAGE_HEIGHT, resources),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        resource,
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref_at)
    return bytes(out)


def _invoice(number: str, issued: str, due: str, rows: list[tuple[str, str, str, str]],
             subtotal: str, tax: str, total: str) -> list[str]:
    rule = "-" * 65
    return [
        "Tumblewick Print Works",
        "9 Lantern Row, Quillmere",
        "Tel 555-0167",
        "",
        "INVOICE",
        "",
        f"Invoice Number:   {number}",
        f"Invoice Date:     {issued}",
        f"Due Date:         {due}",
        "Bill To:          Mapleknot Bakery",
        "",
        "Description                          Qty   Unit Price      Amount",
        rule,
        *(f"{desc:<35}{qty:>4}{unit:>13}{amount:>12}" for desc, qty, unit, amount in rows),
        rule,
        f"{'Subtotal:':>52}{subtotal:>13}",
        f"{'Tax (6%):':>52}{tax:>13}",
        f"{'Total Due:':>52}{total:>13}",
        "",
        "Payment terms: Net 30. Thank you for your business.",
    ]


# The clean invoice: every line, the subtotal, the tax and the total agree.
CLEAN = _invoice("TW-3301", "2026-08-18", "2026-09-17", [
    ("Business cards, box of 250", "4", "38.00", "152.00"),
    ("Poster print, A2 matte", "10", "12.50", "125.00"),
    ("Vinyl window decal", "2", "64.00", "128.00"),
    ("Design proof revision", "1", "45.00", "45.00"),
], "450.00", "27.00", "477.00")

# The planted error: 2 x 36.75 is 73.50, printed as 75.30 (two digits swapped). The wrong
# line is carried into the subtotal, tax and total, so only the line itself shows it.
PLANTED = _invoice("TW-3318", "2026-09-08", "2026-10-08", [
    ("Flyers, A5, pack of 500", "3", "27.40", "82.20"),
    ("Printed tote bags, pack of 10", "2", "36.75", "75.30"),
    ("Foam board mount, A1", "5", "14.20", "71.00"),
    ("Delivery", "1", "12.00", "12.00"),
], "240.50", "14.43", "254.93")

SAMPLES = {
    "tumblewick_TW-3301.pdf": CLEAN,
    "tumblewick_TW-3318.pdf": PLANTED,
}


def write_samples(folder: Path = OUT) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, lines in SAMPLES.items():
        path = folder / name
        path.write_bytes(pdf_bytes(lines))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scan", type=Path, metavar="FILE",
                        help="write a stand-in for a scanned invoice (an image, no text) to FILE instead")
    args = parser.parse_args(argv)
    if args.scan is not None:
        args.scan.parent.mkdir(parents=True, exist_ok=True)
        args.scan.write_bytes(pdf_bytes(scanned=True))
        print(f"Wrote {args.scan.as_posix()} ({args.scan.stat().st_size:,} bytes, no text layer)")
        return 0
    for path in write_samples():
        print(f"Wrote {path.relative_to(ROOT).as_posix()} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
