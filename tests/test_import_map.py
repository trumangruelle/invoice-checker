"""--import-map: the import file's columns renamed, reordered and topped up, through the CLI.

Every test drives `python -m invoicecheck run --import-map ...` through main().
A mapping error has to stop the run before anything is written: a column
quietly missing from a bill import is money in the wrong account.
"""

import json
import unittest

from invoicecheck.cli import EXIT_OK, EXIT_USAGE

from .helpers import ROOT, CliCase

# The mapping file the README points at, shipped in docs/. Using it here means the
# README's example cannot drift away from what the code accepts.
EXAMPLE_MAP = ROOT / "docs" / "import_map_example.json"
EXAMPLE_TARGETS = ["SupplierName", "BillNumber", "BillDate", "DueDate", "LineDescription",
                   "LineQuantity", "LineUnitPrice", "LineAmount", "AccountCode", "TaxCode"]


class ImportMapTests(CliCase):
    def map_file(self, mapping) -> str:
        return str(self.write_json("import_map.json", json.dumps(mapping)))

    def clean_batch(self):
        self.copy_samples("bramblewood_BP-10421.txt", "kestrel_KR-2291.txt")

    def test_columns_are_renamed_reordered_and_topped_up_with_constants(self):
        self.clean_batch()
        code, stdout, _ = self.run_cli("--import-map", str(EXAMPLE_MAP))
        self.assertEqual(code, EXIT_OK)
        rows = self.read_rows("import_ready_mapped.csv")
        self.assertEqual(rows[0], EXAMPLE_TARGETS)
        first = dict(zip(rows[0], rows[1]))
        self.assertEqual(first["SupplierName"], "Bramblewood Paper Co.")
        self.assertEqual(first["BillNumber"], "BP-10421")
        self.assertEqual(first["BillDate"], "2026-08-03")
        self.assertEqual(first["LineUnitPrice"], "42.50")
        self.assertEqual(first["LineAmount"], "255.00")
        # The constants are on every row, not just the first.
        self.assertEqual({r[8] for r in rows[1:]}, {"6000"})
        self.assertEqual({r[9] for r in rows[1:]}, {"PURCH-STD"})
        self.assertIn('import_ready_mapped.csv (10 columns for "Example bill import")', stdout)

    def test_the_mapped_file_has_the_same_rows_as_the_import_file(self):
        self.clean_batch()
        self.run_cli("--import-map", str(EXAMPLE_MAP))
        plain = self.read_rows("import_ready.csv")
        mapped = self.read_rows("import_ready_mapped.csv")
        self.assertEqual(len(mapped), len(plain))
        self.assertEqual(len(mapped), 4 + 4 + 1)   # BP-10421 has 4 lines, KR-2291 has 4
        # Same order, invoice by invoice: row 1 of one file is row 1 of the other.
        self.assertEqual([r[1] for r in mapped[1:]], [r[1] for r in plain[1:]])

    def test_columns_left_out_are_named_on_the_console_not_dropped_in_silence(self):
        self.clean_batch()
        _, stdout, _ = self.run_cli("--import-map", str(EXAMPLE_MAP))
        self.assertIn("left out of the mapped file: Currency, LineNumber, InvoiceSubtotal, "
                      "InvoiceTax, InvoiceTotal, SourceFile", stdout)
        self.assertIn("constant columns: AccountCode, TaxCode", stdout)

    def test_import_ready_csv_keeps_this_tools_own_columns(self):
        # The mapped file is a second file. import_ready.csv is what --history and the
        # review queue read, so renaming its columns would silently break both.
        self.clean_batch()
        _, stdout, _ = self.run_cli("--import-map", str(EXAMPLE_MAP))
        self.assertEqual(self.read_rows("import_ready.csv")[0][:2], ["Vendor", "InvoiceNumber"])
        self.assertIn("import_ready.csv keeps this tool's own column names", stdout)

    def test_a_from_naming_a_column_that_does_not_exist_stops_the_run(self):
        self.clean_batch()
        mapping = {"columns": [{"target": "Supplier", "from": "VendorName"}]}
        code, stdout, stderr = self.run_cli("--import-map", self.map_file(mapping))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("'from' names 'VendorName', which is not a column of import_ready.csv", stderr)
        self.assertIn("The columns are: Vendor, InvoiceNumber", stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(self.out.exists(), "nothing may be written when the mapping is wrong")

    def test_a_mistyped_key_is_reported_rather_than_ignored(self):
        self.clean_batch()
        mapping = {"columns": [{"target": "Supplier", "form": "Vendor"}]}
        code, _, stderr = self.run_cli("--import-map", self.map_file(mapping))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("column 1: unknown key(s) 'form'", stderr)
        self.assertFalse(self.out.exists())

    def test_a_column_with_neither_a_source_nor_a_constant_is_reported(self):
        self.clean_batch()
        mapping = {"columns": [{"target": "Supplier"}]}
        code, _, stderr = self.run_cli("--import-map", self.map_file(mapping))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("give 'from' (a column to copy) or 'value' (a constant to write on every row)",
                      stderr)

    def test_a_column_with_both_a_source_and_a_constant_is_reported(self):
        self.clean_batch()
        mapping = {"columns": [{"target": "Supplier", "from": "Vendor", "value": "Acme"}]}
        code, _, stderr = self.run_cli("--import-map", self.map_file(mapping))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("not both", stderr)

    def test_the_same_target_column_twice_is_reported(self):
        self.clean_batch()
        mapping = {"columns": [{"target": "Supplier", "from": "Vendor"},
                               {"target": "Supplier", "from": "SourceFile"}]}
        code, _, stderr = self.run_cli("--import-map", self.map_file(mapping))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("'Supplier' is already column 1", stderr)

    def test_an_empty_or_shapeless_mapping_is_reported(self):
        self.clean_batch()
        for mapping, expected in (({"columns": []}, '"columns" must be a non-empty list'),
                                  ({}, '"columns" must be a non-empty list'),
                                  ({"columns": [{"target": "", "from": "Vendor"}]},
                                   "'target' must be the column name to write"),
                                  ({"columns": ["Vendor"]}, "each column must be an object"),
                                  ({"colums": []}, "unknown key(s) 'colums' at the top level")):
            with self.subTest(mapping=mapping):
                code, _, stderr = self.run_cli("--import-map", self.map_file(mapping))
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn(expected, stderr)

    def test_a_mapping_file_that_is_not_json_says_where(self):
        self.clean_batch()
        path = self.write_json("import_map.json", '{"columns": [ {"target": "A", "from": "Vendor"} ,]}')
        code, _, stderr = self.run_cli("--import-map", str(path))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("it is not valid JSON (line 1, column", stderr)

    def test_a_missing_mapping_file_says_so(self):
        self.clean_batch()
        code, _, stderr = self.run_cli("--import-map", str(self.work / "nope.json"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("import map not found", stderr)

    def test_no_mapped_file_is_written_without_the_option(self):
        self.clean_batch()
        self.run_cli()
        self.assertFalse((self.out / "import_ready_mapped.csv").exists())

    def test_a_constant_that_looks_like_a_formula_is_neutralised(self):
        self.clean_batch()
        mapping = {"columns": [{"target": "Supplier", "from": "Vendor"},
                               {"target": "Account", "value": "=1+1"}]}
        self.run_cli("--import-map", self.map_file(mapping))
        rows = self.read_rows("import_ready_mapped.csv")
        self.assertEqual({r[1] for r in rows[1:]}, {"'=1+1"})

    def test_a_mapped_file_handed_back_as_history_is_refused_not_ignored(self):
        # The mapped file has no InvoiceNumber column, so --history would match nothing
        # and invoices that came back a second time would stop being caught in silence.
        self.clean_batch()
        self.run_cli("--import-map", str(EXAMPLE_MAP))
        history = self.out / "import_ready_mapped.csv"
        code, _, stderr = self.run_cli("--history", str(history))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("has no 'InvoiceNumber' column, so it would flag nothing", stderr)
        self.assertIn("Its columns are: SupplierName, BillNumber", stderr)

    def test_the_unmapped_import_file_still_works_as_history(self):
        self.clean_batch()
        self.run_cli("--import-map", str(EXAMPLE_MAP))
        history = self.work / "last_month.csv"
        history.write_text((self.out / "import_ready.csv").read_text(encoding="utf-8"),
                           encoding="utf-8")
        code, stdout, _ = self.run_cli("--history", str(history))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("0 PASS, 2 NEEDS REVIEW", stdout)
        self.assertIn("seen in an earlier run", stdout)

    def test_an_approved_invoice_reaches_the_mapped_file_too(self):
        self.copy_samples("mossgate_MG-5498.html")
        self.run_cli()
        self.cli("approve", "--results", self.out / "results.json", "--file", "mossgate_MG-5498.html",
                 "--by", "Dana Fell", "--approvals", self.queue)
        mapping = {"columns": [{"target": "Supplier", "from": "Vendor"},
                               {"target": "Bill", "from": "InvoiceNumber"}]}
        code, _, _ = self.run_cli("--approvals", self.queue, "--import-map", self.map_file(mapping))
        self.assertEqual(code, EXIT_OK)
        rows = self.read_rows("import_ready_mapped.csv")
        self.assertEqual(rows[0], ["Supplier", "Bill"])
        self.assertEqual({r[1] for r in rows[1:]}, {"MG-5498"})


if __name__ == "__main__":
    unittest.main()
