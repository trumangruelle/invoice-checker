"""Loading files: what is read, what is listed as not checked, and decoding problems."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from invoicecheck import pdftext
from invoicecheck.documents import load_document, load_folder, scan_folder


class ScanFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name, data=b"x"):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_every_file_is_either_read_or_listed_with_a_reason(self):
        for name in ("a.txt", "B.HTML", "sub/c.htm", "scan_0001.pdf", "photo.JPG", "notes.docx"):
            self.touch(name)
        readable, unread = scan_folder(self.folder)
        # A PDF is tried; whether it can be read is decided when it is loaded (tests/test_pdf.py).
        self.assertEqual([p.relative_to(self.folder).as_posix() for p in readable],
                         ["a.txt", "B.HTML", "scan_0001.pdf", "sub/c.htm"])
        self.assertEqual([(u.name, u.reason) for u in unread], [
            ("notes.docx", "not read: only .txt, .html and .pdf invoices are checked"),
            ("photo.JPG", "image, not read: it needs OCR first"),
        ])

    def test_hidden_and_system_files_are_not_listed(self):
        for name in ("a.txt", ".DS_Store", ".hidden.pdf", "._a.txt", ".git/config", "Thumbs.db",
                     "desktop.ini", "~$invoice.docx"):
            self.touch(name)
        readable, unread = scan_folder(self.folder)
        self.assertEqual([p.name for p in readable], ["a.txt"])
        self.assertEqual(unread, [])

    def test_files_in_subfolders_are_listed_by_their_relative_path(self):
        self.touch("a.txt")
        self.touch("august/photo.jpg")
        _, unread = scan_folder(self.folder)
        self.assertEqual([u.name for u in unread], ["august/photo.jpg"])

    def test_a_pdf_that_cannot_be_read_joins_the_not_checked_list_in_order(self):
        for name in ("a.txt", "august/scan.pdf", "b_photo.jpg", "z.docx"):
            self.touch(name)
        with mock.patch.object(pdftext, "pypdf", None):
            docs, unread = load_folder(self.folder)
        self.assertEqual([d.name for d in docs], ["a.txt"])
        self.assertEqual([(u.name, u.reason) for u in unread], [
            ("august/scan.pdf", "install pypdf to read PDF invoices"),
            ("b_photo.jpg", "image, not read: it needs OCR first"),
            ("z.docx", "not read: only .txt, .html and .pdf invoices are checked"),
        ])


class DecodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "invoice.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_utf8_with_or_without_a_byte_order_mark_has_no_warning(self):
        for data in ("Café Supplies\n".encode("utf-8"), "Café Supplies\n".encode("utf-8-sig")):
            self.path.write_bytes(data)
            doc = load_document(self.path)
            self.assertEqual(doc.text, "Café Supplies\n")
            self.assertEqual(doc.warnings, [])

    def test_bytes_that_are_not_utf8_are_replaced_and_reported(self):
        self.path.write_bytes("Café Supplies – Invoice\n".encode("cp1252"))
        doc = load_document(self.path)
        self.assertIn("\ufffd", doc.text)
        self.assertEqual(len(doc.warnings), 1)
        self.assertIn("not valid UTF-8", doc.warnings[0])
        self.assertIn("2 unreadable character(s)", doc.warnings[0])


if __name__ == "__main__":
    unittest.main()
