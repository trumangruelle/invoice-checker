"""Each verification rule, tested on both sides of its line."""

import unittest
from datetime import date, timedelta

from invoicecheck.model import LineItem
from invoicecheck.verify import (
    CHECKS,
    LABELS,
    NEEDS_REVIEW,
    PASS,
    MoneySummary,
    check_grounding,
    money_summary,
    verify_all,
    verify_invoice,
)

from .helpers import TODAY, D, make_invoice


def codes(findings):
    return [f.check for f in findings]


class BaselineTests(unittest.TestCase):
    def test_consistent_invoice_passes(self):
        verdict = verify_all([make_invoice()], TODAY)[0]
        self.assertEqual(verdict.findings, [])
        self.assertEqual(verdict.status, PASS)

    def test_any_finding_means_needs_review(self):
        verdict = verify_all([make_invoice(total=D("40.00"))], TODAY)[0]
        self.assertEqual(verdict.status, NEEDS_REVIEW)


class ExtractionAndRequiredFieldTests(unittest.TestCase):
    def test_extractor_warnings_are_reasons(self):
        findings = verify_invoice(make_invoice(warnings=["line 9: could not read an item row"]), TODAY)
        self.assertEqual(codes(findings), ["extraction"])
        self.assertIn("line 9", findings[0].message)

    def test_each_missing_field_is_named(self):
        inv = make_invoice(vendor=None, invoice_number=None, invoice_date=None, due_date=None,
                           line_items=[], subtotal=None, tax=None, tax_rate=None, total=None)
        messages = [f.message for f in verify_invoice(inv, TODAY) if f.check == "required_fields"]
        self.assertEqual(messages, ["No vendor name found.", "No invoice number found.",
                                    "No invoice date found.", "No line items found.", "No total found."])

    def test_unreadable_invoice_date_is_quoted(self):
        inv = make_invoice(invoice_date=None, invoice_date_raw="31/31/2026")
        messages = [f.message for f in verify_invoice(inv, TODAY)]
        self.assertIn("Invoice date '31/31/2026' is not a date I can read.", messages)

    def test_subtotal_is_optional_when_lines_make_the_total(self):
        inv = make_invoice(subtotal=None, tax=None, tax_rate=None, total=D("36.50"))
        self.assertEqual(verify_invoice(inv, TODAY), [])


class LineChecksTests(unittest.TestCase):
    def test_line_math_flags_the_item(self):
        items = [LineItem("Managed support", D("12"), D("35.00"), D("435.00")),
                 LineItem("Backup", D("1"), D("1.50"), D("1.50"))]
        inv = make_invoice(line_items=items, subtotal=D("436.50"), tax=None, tax_rate=None,
                           total=D("436.50"))
        findings = verify_invoice(inv, TODAY)
        self.assertEqual(codes(findings), ["line_math"])
        self.assertIn("12 x $35.00 = $420.00, but the line shows $435.00", findings[0].message)
        self.assertIn("Item 1", findings[0].message)

    def test_line_math_allows_one_cent_of_rounding_but_not_two(self):
        # 3 x 3.333 = 9.999 -> 10.00
        ok = make_invoice(line_items=[LineItem("A", D("3"), D("3.333"), D("10.01"))],
                          subtotal=D("10.01"), tax=None, tax_rate=None, total=D("10.01"))
        self.assertEqual(verify_invoice(ok, TODAY), [])
        bad = make_invoice(line_items=[LineItem("A", D("3"), D("3.333"), D("10.02"))],
                           subtotal=D("10.02"), tax=None, tax_rate=None, total=D("10.02"))
        self.assertEqual(codes(verify_invoice(bad, TODAY)), ["line_math"])

    def test_line_math_message_keeps_a_sub_cent_unit_price(self):
        items = [LineItem("Cloud backup, per GB", D("320"), D("0.125"), D("41.60"))]
        inv = make_invoice(line_items=items, subtotal=D("41.60"), tax=None, tax_rate=None, total=D("41.60"))
        findings = verify_invoice(inv, TODAY)
        self.assertEqual(codes(findings), ["line_math"])
        self.assertIn("320 x $0.125 = $40.00, but the line shows $41.60", findings[0].message)

    def test_lump_sum_line_is_accepted(self):
        inv = make_invoice(line_items=[LineItem("Call-out", None, None, D("36.50"))])
        self.assertEqual(verify_invoice(inv, TODAY), [])

    def test_half_filled_line_is_incomplete(self):
        inv = make_invoice(line_items=[LineItem("Thing", D("2"), None, D("36.50"))])
        findings = verify_invoice(inv, TODAY)
        self.assertEqual(codes(findings), ["line_incomplete"])
        self.assertIn("has a quantity but no unit price", findings[0].message)

    def test_line_without_amount_is_incomplete(self):
        items = [LineItem("Thing", D("2"), D("10.00"), None)]
        self.assertIn("line_incomplete", codes(verify_invoice(make_invoice(line_items=items), TODAY)))

    def test_lines_must_sum_to_subtotal(self):
        inv = make_invoice(subtotal=D("45.50"), tax_rate=None, total=D("48.42"))
        findings = verify_invoice(inv, TODAY)
        self.assertEqual(codes(findings), ["lines_sum"])
        self.assertEqual(findings[0].message,
                         "Line items add up to $36.50 but the subtotal says $45.50 (off by $9.00).")

    def test_without_subtotal_lines_plus_tax_must_make_the_total(self):
        inv = make_invoice(subtotal=None, tax=D("2.92"), tax_rate=None, total=D("49.42"))
        findings = verify_invoice(inv, TODAY)
        self.assertEqual(codes(findings), ["lines_sum"])
        self.assertIn("no subtotal is printed", findings[0].message)


class TaxAndTotalTests(unittest.TestCase):
    def test_tax_must_match_the_printed_rate(self):
        findings = verify_invoice(make_invoice(tax=D("2.00"), total=D("38.50")), TODAY)
        self.assertEqual(codes(findings), ["tax_rate"])
        self.assertEqual(findings[0].message,
                         "Tax is $2.00, but 8% of the $36.50 subtotal is $2.92 (off by $0.92).")

    def test_tax_rate_tolerates_per_line_rounding(self):
        # Two lines -> one cent allowed; 2.93 is within it.
        self.assertEqual(verify_invoice(make_invoice(tax=D("2.93"), total=D("39.43")), TODAY), [])
        self.assertEqual(codes(verify_invoice(make_invoice(tax=D("2.94"), total=D("39.44")), TODAY)),
                         ["tax_rate"])

    def test_no_rate_printed_means_no_rate_check(self):
        inv = make_invoice(tax=D("2.00"), tax_rate=None, total=D("38.50"))
        self.assertEqual(verify_invoice(inv, TODAY), [])

    def test_subtotal_plus_tax_must_equal_total(self):
        findings = verify_invoice(make_invoice(total=D("49.42")), TODAY)
        self.assertEqual(codes(findings), ["total_math"])
        self.assertEqual(findings[0].message,
                         "Subtotal $36.50 + tax $2.92 = $39.42, but the total says $49.42 (off by $10.00).")

    def test_total_within_one_cent_passes_two_cents_fails(self):
        self.assertEqual(verify_invoice(make_invoice(total=D("39.43")), TODAY), [])
        self.assertEqual(codes(verify_invoice(make_invoice(total=D("39.44")), TODAY)), ["total_math"])

    def test_zero_or_negative_total_needs_a_person(self):
        items = [LineItem("Refund", D("1"), D("-5.00"), D("-5.00"))]
        inv = make_invoice(line_items=items, subtotal=D("-5.00"), tax=None, tax_rate=None, total=D("-5.00"))
        self.assertEqual(codes(verify_invoice(inv, TODAY)), ["total_not_positive"])

    def test_zero_total_needs_a_person_one_cent_does_not(self):
        def lump(amount):
            return make_invoice(line_items=[LineItem("Adjustment", None, None, D(amount))],
                                subtotal=D(amount), tax=None, tax_rate=None, total=D(amount))
        self.assertEqual(codes(verify_invoice(lump("0.00"), TODAY)), ["total_not_positive"])
        self.assertEqual(verify_invoice(lump("0.01"), TODAY), [])


class DateTests(unittest.TestCase):
    def test_future_date(self):
        inv = make_invoice(invoice_date=date(2062, 9, 18), due_date=date(2062, 10, 18))
        findings = verify_invoice(inv, TODAY)
        self.assertEqual(codes(findings), ["date_future"])
        self.assertIn("2062-09-18 is in the future", findings[0].message)

    def test_today_is_not_the_future(self):
        self.assertEqual(verify_invoice(make_invoice(invoice_date=TODAY, due_date=None), TODAY), [])

    def test_stale_date_and_switching_it_off(self):
        inv = make_invoice(invoice_date=date(2025, 1, 2), due_date=None)
        findings = verify_invoice(inv, TODAY, max_age_days=365)
        self.assertEqual(codes(findings), ["date_stale"])
        self.assertIn("627 days old", findings[0].message)
        self.assertEqual(verify_invoice(inv, TODAY, max_age_days=0), [])

    def test_stale_boundary_365_days_passes_366_fails(self):
        on_limit = make_invoice(invoice_date=TODAY - timedelta(days=365), due_date=None)
        self.assertEqual(verify_invoice(on_limit, TODAY, max_age_days=365), [])
        over = make_invoice(invoice_date=TODAY - timedelta(days=366), due_date=None)
        findings = verify_invoice(over, TODAY, max_age_days=365)
        self.assertEqual(codes(findings), ["date_stale"])
        self.assertIn("366 days old (limit 365)", findings[0].message)

    def test_due_before_issue(self):
        inv = make_invoice(invoice_date=date(2026, 9, 10), due_date=date(2026, 9, 1))
        self.assertEqual(codes(verify_invoice(inv, TODAY)), ["due_before_issue"])

    def test_unreadable_due_date(self):
        inv = make_invoice(due_date=None, due_date_raw="on receipt-ish")
        self.assertEqual(codes(verify_invoice(inv, TODAY)), ["date_unreadable"])


class DuplicateTests(unittest.TestCase):
    def test_same_vendor_and_number_flags_every_copy(self):
        a = make_invoice(source_file="a.html")
        b = make_invoice(source_file="b.html")
        verdicts = verify_all([a, b], TODAY)
        self.assertEqual([codes(v.findings) for v in verdicts], [["duplicate_number"], ["duplicate_number"]])
        self.assertIn("also appears in b.html", verdicts[0].findings[0].message)
        self.assertIn("also appears in a.html", verdicts[1].findings[0].message)

    def test_number_and_vendor_formatting_is_normalised(self):
        a = make_invoice(source_file="a.txt", vendor="Mossgate Janitorial Services LLC", invoice_number="MG-5512")
        b = make_invoice(source_file="b.txt", vendor="MOSSGATE JANITORIAL SERVICES", invoice_number="mg 5512")
        self.assertEqual([codes(v.findings) for v in verify_all([a, b], TODAY)],
                         [["duplicate_number"], ["duplicate_number"]])

    def test_same_number_from_different_vendors_is_fine(self):
        a = make_invoice(source_file="a.txt", vendor="Alpha Supplies")
        b = make_invoice(source_file="b.txt", vendor="Beta Services", invoice_date=date(2026, 9, 5),
                         due_date=date(2026, 10, 5), line_items=[LineItem("Widget", D("1"), D("10.00"), D("10.00"))],
                         subtotal=D("10.00"), tax=D("0.80"), total=D("10.80"))
        self.assertEqual([v.findings for v in verify_all([a, b], TODAY)], [[], []])

    def test_same_number_and_total_under_another_vendor_name_is_a_possible_duplicate(self):
        # One invoice whose vendor was read two ways, e.g. a banner line on a resent copy.
        a = make_invoice(source_file="a.txt", vendor="Bramblewood Paper Co.")
        b = make_invoice(source_file="b.txt", vendor="PAYMENT REMINDER")
        verdicts = verify_all([a, b], TODAY)
        self.assertEqual([codes(v.findings) for v in verdicts], [["possible_duplicate"], ["possible_duplicate"]])
        self.assertIn("b.txt (vendor 'PAYMENT REMINDER', same total and date)", verdicts[0].findings[0].message)
        self.assertIn("a.txt (vendor 'Bramblewood Paper Co.', same total and date)", verdicts[1].findings[0].message)

    def test_same_number_under_another_vendor_name_matches_on_total_or_date(self):
        a = make_invoice(source_file="a.txt", vendor="Alpha Supplies")
        same_total = make_invoice(source_file="b.txt", vendor="Beta Services", invoice_date=date(2026, 9, 5),
                                  due_date=None)
        same_date = make_invoice(source_file="c.txt", vendor="Gamma Goods",
                                 line_items=[LineItem("Widget", D("1"), D("10.00"), D("10.00"))],
                                 subtotal=D("10.00"), tax=D("0.80"), total=D("10.80"))
        self.assertIn("b.txt (vendor 'Beta Services', same total)",
                      verify_all([a, same_total], TODAY)[0].findings[0].message)
        self.assertIn("c.txt (vendor 'Gamma Goods', same date)",
                      verify_all([a, same_date], TODAY)[0].findings[0].message)

    def test_same_vendor_date_and_total_under_new_number(self):
        a = make_invoice(source_file="a.txt", invoice_number="T-100")
        b = make_invoice(source_file="b.txt", invoice_number="T-101")
        verdicts = verify_all([a, b], TODAY)
        self.assertEqual([codes(v.findings) for v in verdicts], [["possible_duplicate"], ["possible_duplicate"]])
        self.assertIn("b.txt (invoice T-101)", verdicts[0].findings[0].message)

    def test_history_file_flags_already_seen(self):
        history = {("TESTVENDOR", "T100")}
        verdict = verify_all([make_invoice()], TODAY, history=history)[0]
        self.assertEqual(codes(verdict.findings), ["already_seen"])


class OverbillingTests(unittest.TestCase):
    """How much each held invoice asks for beyond what its own figures support."""

    def verdict(self, **overrides):
        return verify_all([make_invoice(**overrides)], TODAY)[0]

    def test_a_consistent_invoice_is_not_overbilled(self):
        self.assertEqual(self.verdict().overcharge, D("0"))

    def test_a_wrong_line_carried_through_to_the_total_counts_its_difference(self):
        items = [LineItem("Widget", D("2"), D("10.00"), D("25.00")),
                 LineItem("Gadget", D("3"), D("5.50"), D("16.50"))]
        v = self.verdict(line_items=items, subtotal=D("41.50"), tax=D("3.32"), total=D("44.82"))
        self.assertEqual(codes(v.findings), ["line_math"])
        self.assertEqual((v.findings[0].overcharge, v.overcharge), (D("5.00"), D("5.00")))

    def test_a_wrong_line_that_the_subtotal_ignores_is_not_overbilling(self):
        # The line says 25.00 but the subtotal and total are the correct ones: nothing is overpaid.
        items = [LineItem("Widget", D("2"), D("10.00"), D("25.00")),
                 LineItem("Gadget", D("3"), D("5.50"), D("16.50"))]
        v = self.verdict(line_items=items)
        self.assertEqual(codes(v.findings), ["line_math", "lines_sum"])
        self.assertEqual([f.overcharge for f in v.findings], [D("5.00"), D("-5.00")])
        self.assertEqual(v.overcharge, D("0"))

    def test_subtotal_and_total_differences_count(self):
        self.assertEqual(self.verdict(subtotal=D("45.50"), tax=D("3.64"), total=D("49.14")).overcharge, D("9.00"))
        self.assertEqual(self.verdict(total=D("49.42")).overcharge, D("10.00"))

    def test_an_undercharge_is_held_but_not_counted(self):
        v = self.verdict(tax=D("2.00"), total=D("38.50"))
        self.assertEqual(codes(v.findings), ["tax_rate"])
        self.assertEqual(v.findings[0].overcharge, D("-0.92"))
        self.assertEqual((v.status, v.overcharge), (NEEDS_REVIEW, D("0")))

    def test_not_counted_when_the_invoice_was_not_fully_read(self):
        # An unread shipping line makes total_math fire; that difference is not known to be overbilling.
        v = self.verdict(total=D("49.42"), warnings=["totals: an amount that is not a subtotal, tax or total"])
        self.assertEqual(codes(v.findings), ["extraction", "total_math"])
        self.assertEqual(v.overcharge, D("0"))

    def test_the_later_copy_of_a_duplicate_is_sent_twice_not_overbilled(self):
        verdicts = verify_all([make_invoice(source_file="a.html"), make_invoice(source_file="b.html")], TODAY)
        self.assertEqual([v.repeat_total for v in verdicts], [D("0"), D("39.42")])
        self.assertEqual([v.overcharge for v in verdicts], [D("0"), D("0")])
        self.assertEqual([v.is_repeat_copy for v in verdicts], [False, True])
        self.assertEqual([f.overcharge for v in verdicts for f in v.findings], [None, None])
        self.assertEqual(money_summary(verdicts),
                         MoneySummary(held_invoices=2, held_total=D("78.84"), overcharge=D("0"),
                                      repeat_copies=1, repeat_total=D("39.42"),
                                      approved_invoices=0, approved_total=D("0"),
                                      approved_overcharge=D("0")))

    def test_an_invoice_already_in_the_history_is_a_repeat_copy(self):
        verdict = verify_all([make_invoice()], TODAY, history={("TESTVENDOR", "T100")})[0]
        self.assertEqual((verdict.repeat_total, verdict.overcharge), (D("39.42"), D("0")))
        self.assertEqual(money_summary([verdict]).repeat_copies, 1)

    def test_an_arithmetic_error_sent_twice_is_counted_once(self):
        # Both copies overcharge by $10.00, but it is one invoice: the $10.00 counts on the first
        # copy, and the second copy is reported as sent twice instead.
        verdicts = verify_all([make_invoice(source_file="a.txt", total=D("49.42")),
                               make_invoice(source_file="b.txt", total=D("49.42"))], TODAY)
        self.assertEqual([codes(v.findings) for v in verdicts],
                         [["total_math", "duplicate_number"], ["total_math", "duplicate_number"]])
        self.assertEqual([(v.overcharge, v.repeat_total) for v in verdicts],
                         [(D("10.00"), D("0")), (D("0"), D("49.42"))])
        self.assertEqual(money_summary(verdicts),
                         MoneySummary(held_invoices=2, held_total=D("98.84"), overcharge=D("10.00"),
                                      repeat_copies=1, repeat_total=D("49.42"),
                                      approved_invoices=0, approved_total=D("0"),
                                      approved_overcharge=D("0")))

    def test_batch_summary_keeps_overbilling_and_invoices_sent_twice_apart(self):
        verdicts = verify_all([
            make_invoice(source_file="ok.txt", invoice_number="A-1", invoice_date=date(2026, 9, 2)),
            make_invoice(source_file="total.txt", invoice_number="A-2", invoice_date=date(2026, 9, 3),
                         total=D("49.42")),
            make_invoice(source_file="copy1.txt", invoice_number="A-3", invoice_date=date(2026, 9, 4)),
            make_invoice(source_file="copy2.txt", invoice_number="A-3", invoice_date=date(2026, 9, 4)),
        ], TODAY)
        self.assertEqual([v.status for v in verdicts], [PASS, NEEDS_REVIEW, NEEDS_REVIEW, NEEDS_REVIEW])
        self.assertEqual(money_summary(verdicts),
                         MoneySummary(held_invoices=3, held_total=D("128.26"), overcharge=D("10.00"),
                                      repeat_copies=1, repeat_total=D("39.42"),
                                      approved_invoices=0, approved_total=D("0"),
                                      approved_overcharge=D("0")))

    def test_every_check_has_a_plain_label(self):
        self.assertEqual(list(LABELS), list(CHECKS))


class GroundingTests(unittest.TestCase):
    SOURCE ="Invoice T-100. Widget 2 x 10.00 = 20.00. Gadget 3 x 5.50 = 16.50. " \
             "Subtotal 36.50 Tax 8% 2.92 Total 39.42"

    def test_ai_values_printed_in_the_source_pass(self):
        inv = make_invoice(extractor="ai:claude-opus-5", layout="ai")
        self.assertEqual(check_grounding(inv, self.SOURCE), [])
        self.assertEqual(verify_invoice(inv, TODAY, source_text=self.SOURCE), [])

    def test_ai_value_not_in_the_source_is_flagged(self):
        inv = make_invoice(extractor="ai:claude-opus-5", layout="ai", invoice_number="T-999")
        findings = check_grounding(inv, self.SOURCE.replace("Total 39.42", "Total 39.24"))
        self.assertEqual(codes(findings), ["not_in_source", "not_in_source"])
        self.assertIn("total 39.42", findings[0].message)
        self.assertIn("'T-999'", findings[1].message)

    def test_ai_quantity_not_in_the_source_is_flagged(self):
        items = [LineItem("Widget", D("7"), D("10.00"), D("20.00")),
                 LineItem("Gadget", D("3"), D("5.50"), D("16.50"))]
        inv = make_invoice(extractor="ai:claude-opus-5", layout="ai", line_items=items)
        findings = check_grounding(inv, self.SOURCE)
        self.assertEqual(codes(findings), ["not_in_source"])
        self.assertIn("item 1 quantity 7,", findings[0].message)

    def test_rules_extractions_are_not_grounding_checked(self):
        inv = make_invoice()
        self.assertEqual(verify_invoice(inv, TODAY, source_text="nothing here"), [])


if __name__ == "__main__":
    unittest.main()
