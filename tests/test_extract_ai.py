"""The optional AI path, tested without the network.

Requests are built and inspected but never sent: a FakeOpener stands in for
urllib.request.urlopen and returns fixture responses from tests/fixtures/.
"""

import http.client
import json
import unittest
import urllib.error
from datetime import date
from decimal import Decimal

from invoicecheck import extract_ai
from invoicecheck.cli import extract_document
from invoicecheck.extract_ai import (
    API_URL,
    FALLBACK_BETA,
    AIExtractionError,
    build_request,
    build_request_body,
    call_messages_api,
    extract_with_ai,
    parse_response,
)
from invoicecheck.verify import verify_all

from .helpers import TODAY, FakeOpener, fixture_doc, fixture_json, http_error, sample

FAKE_KEY = "test-key-not-real"


def no_sleep(_seconds):
    pass


class RequestBuildingTests(unittest.TestCase):
    def setUp(self):
        self.doc = sample("kestrel_KR-2304.txt")
        self.body = build_request_body(self.doc.text, self.doc.name)
        self.request = build_request(self.body, FAKE_KEY)

    def test_endpoint_method_and_headers(self):
        self.assertEqual(self.request.full_url, API_URL)
        self.assertEqual(self.request.get_method(), "POST")
        # urllib stores header names capitalised.
        self.assertEqual(self.request.get_header("X-api-key"), FAKE_KEY)
        self.assertEqual(self.request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(self.request.get_header("Content-type"), "application/json")
        self.assertEqual(self.request.get_header("Anthropic-beta"), FALLBACK_BETA)

    def test_body_shape(self):
        sent = json.loads(self.request.data.decode("utf-8"))
        self.assertEqual(sent["model"], "claude-opus-5")
        self.assertEqual(sent["fallbacks"], "default")
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        schema = sent["output_config"]["format"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(sent["messages"][0]["role"], "user")
        self.assertIn("| MS-100 | Managed support, per seat  | 12  | $35.00  | $435.00    |",
                      sent["messages"][0]["content"])
        self.assertIn("Do not correct arithmetic", sent["system"])

    def test_key_is_only_in_the_header(self):
        self.assertNotIn(FAKE_KEY, self.request.data.decode("utf-8"))

    def test_other_models_get_no_fallback_parameter(self):
        body = build_request_body("text", "x.txt", model="claude-sonnet-5")
        self.assertNotIn("fallbacks", body)
        self.assertIsNone(build_request(body, FAKE_KEY).get_header("Anthropic-beta"))

    def test_missing_key_is_refused_before_any_request(self):
        with self.assertRaises(AIExtractionError):
            build_request(self.body, "")


class ResponseParsingTests(unittest.TestCase):
    def test_fixture_maps_onto_the_shared_schema(self):
        inv = parse_response(fixture_json("messages_response_kr2304.json"), "kestrel_KR-2304.txt")
        self.assertEqual(inv.extractor, "ai:claude-opus-5")
        self.assertEqual(inv.vendor, "Kestrelrook IT Services")
        self.assertEqual(inv.invoice_number, "KR-2304")
        self.assertEqual(inv.invoice_date, date(2026, 8, 25))
        self.assertEqual(inv.due_date, date(2026, 9, 24))
        self.assertEqual(len(inv.line_items), 3)
        self.assertEqual(inv.line_items[0].amount, Decimal("435.00"))
        self.assertEqual(inv.line_items[2].quantity, Decimal("1.5"))
        self.assertEqual((inv.subtotal, inv.tax, inv.tax_rate, inv.total),
                         (Decimal("597.49"), Decimal("35.85"), Decimal("6"), Decimal("633.34")))
        self.assertEqual(inv.warnings, [])

    def test_same_output_schema_as_the_rules_extractor(self):
        from invoicecheck.extract_rules import extract
        ai = parse_response(fixture_json("messages_response_kr2304.json"), "kestrel_KR-2304.txt").to_dict()
        rules = extract(sample("kestrel_KR-2304.txt")).to_dict()
        self.assertEqual(set(ai), set(rules))
        for field in ("vendor", "invoice_number", "invoice_date", "due_date", "subtotal", "tax",
                      "tax_rate_percent", "total"):
            self.assertEqual(ai[field], rules[field], field)
        strip = lambda items: [{k: v for k, v in i.items() if k != "source_line"} for i in items]
        self.assertEqual(strip(ai["line_items"]), strip(rules["line_items"]))

    def test_refusal_is_an_error(self):
        with self.assertRaisesRegex(AIExtractionError, "refusal"):
            parse_response(fixture_json("messages_response_refusal.json"), "x.txt")

    def test_truncated_response_is_an_error(self):
        with self.assertRaisesRegex(AIExtractionError, "max_tokens"):
            parse_response(fixture_json("messages_response_max_tokens.json"), "x.txt")

    def test_non_json_text_is_an_error(self):
        payload = fixture_json("messages_response_kr2304.json")
        payload["content"][-1]["text"] = "Here is the invoice: vendor Kestrel"
        with self.assertRaisesRegex(AIExtractionError, "not valid JSON"):
            parse_response(payload, "x.txt")

    def test_unreadable_values_become_warnings(self):
        payload = fixture_json("messages_response_kr2304.json")
        data = json.loads(payload["content"][-1]["text"])
        data["total"] = "six hundred"
        payload["content"][-1]["text"] = json.dumps(data)
        inv = parse_response(payload, "x.txt")
        self.assertIsNone(inv.total)
        self.assertEqual(inv.warnings, ["AI returned an unreadable total: 'six hundred'"])


class TransportTests(unittest.TestCase):
    def test_success(self):
        opener = FakeOpener(fixture_json("messages_response_kr2304.json"))
        inv = extract_with_ai(sample("kestrel_KR-2304.txt"), FAKE_KEY, opener=opener, sleep=no_sleep)
        self.assertEqual(inv.invoice_number, "KR-2304")
        self.assertEqual(len(opener.requests), 1)

    def test_overloaded_is_retried_then_succeeds(self):
        delays = []
        opener = FakeOpener(http_error(529, fixture_json("messages_error_overloaded.json"), retry_after="3"),
                            fixture_json("messages_response_kr2304.json"))
        payload = call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=delays.append)
        self.assertEqual(payload["id"], "msg_fixture_kr2304")
        self.assertEqual(delays, [3.0])

    def test_retries_are_bounded(self):
        overloaded = fixture_json("messages_error_overloaded.json")
        opener = FakeOpener(http_error(529, overloaded), http_error(529, overloaded), http_error(529, overloaded))
        with self.assertRaisesRegex(AIExtractionError, "HTTP 529 .overloaded_error: Overloaded."):
            call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=no_sleep)
        self.assertEqual(len(opener.requests), 3)

    def test_bad_request_is_not_retried(self):
        opener = FakeOpener(http_error(400, fixture_json("messages_error_invalid_request.json")))
        with self.assertRaisesRegex(AIExtractionError, "invalid_request_error"):
            call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=no_sleep)
        self.assertEqual(len(opener.requests), 1)

    def test_connection_error_after_retries(self):
        opener = FakeOpener(*(urllib.error.URLError("no route") for _ in range(3)))
        with self.assertRaisesRegex(AIExtractionError, "could not reach"):
            call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=no_sleep)

    def test_read_timeout_is_retried_then_reported(self):
        # urllib raises a timeout while waiting for the response unwrapped, not as URLError.
        opener = FakeOpener(*(TimeoutError("The read operation timed out") for _ in range(3)))
        with self.assertRaisesRegex(AIExtractionError,
                                    r"could not reach the Messages API \(TimeoutError: The read operation timed out\)"):
            call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=no_sleep)
        self.assertEqual(len(opener.requests), 3)

    def test_dropped_connections_are_retried_then_succeed(self):
        opener = FakeOpener(http.client.RemoteDisconnected("Remote end closed connection without response"),
                            ConnectionResetError(10054, "An existing connection was forcibly closed"),
                            fixture_json("messages_response_kr2304.json"))
        payload = call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=no_sleep)
        self.assertEqual(payload["id"], "msg_fixture_kr2304")
        self.assertEqual(len(opener.requests), 3)

    def test_incomplete_read_is_reported(self):
        opener = FakeOpener(*(http.client.IncompleteRead(b"{", 100) for _ in range(3)))
        with self.assertRaisesRegex(AIExtractionError, "IncompleteRead"):
            call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=no_sleep)

    def test_error_messages_never_contain_the_key(self):
        opener = FakeOpener(http_error(401, {"type": "error", "error": {"type": "authentication_error",
                                                                          "message": "invalid x-api-key"}}))
        with self.assertRaises(AIExtractionError) as caught:
            call_messages_api({"model": "m"}, FAKE_KEY, opener=opener, sleep=no_sleep)
        self.assertNotIn(FAKE_KEY, str(caught.exception))


class VerificationOfAIOutputTests(unittest.TestCase):
    def test_ai_extraction_still_catches_the_printed_error(self):
        doc = sample("kestrel_KR-2304.txt")
        inv = extract_with_ai(doc, FAKE_KEY, opener=FakeOpener(fixture_json("messages_response_kr2304.json")),
                              sleep=no_sleep)
        verdict = verify_all([inv], TODAY, sources={doc.name: doc.text})[0]
        self.assertEqual([f.check for f in verdict.findings], ["line_math"])

    def test_invented_number_is_caught_by_grounding(self):
        payload = fixture_json("messages_response_kr2304.json")
        data = json.loads(payload["content"][-1]["text"])
        # A "helpful" model fixes the line and the totals so the arithmetic is consistent.
        data["line_items"][0]["amount"] = "420.00"
        data["subtotal"], data["tax"], data["total"] = "582.49", "34.95", "617.44"
        payload["content"][-1]["text"] = json.dumps(data)
        doc = sample("kestrel_KR-2304.txt")
        inv = extract_with_ai(doc, FAKE_KEY, opener=FakeOpener(payload), sleep=no_sleep)
        verdict = verify_all([inv], TODAY, sources={doc.name: doc.text})[0]
        checks = [f.check for f in verdict.findings]
        self.assertNotIn("line_math", checks)       # the arithmetic now looks fine...
        self.assertEqual(checks.count("not_in_source"), 4)   # ...but those numbers are not on the page


class AutoModeTests(unittest.TestCase):
    def test_auto_uses_ai_only_for_files_the_rules_cannot_read(self):
        opener = FakeOpener(fixture_json("messages_response_glasswing.json"))
        known = extract_document(sample("kestrel_KR-2291.txt"), "auto", api_key=FAKE_KEY, model=None,
                                 opener=opener, sleep=no_sleep)
        self.assertEqual(known.extractor, "rules")
        self.assertEqual(opener.requests, [])

        doc = fixture_doc("glasswing_email_invoice.txt")
        unknown = extract_document(doc, "auto", api_key=FAKE_KEY, model=None, opener=opener, sleep=no_sleep)
        self.assertEqual(unknown.extractor, "ai:claude-opus-5")
        self.assertEqual(len(opener.requests), 1)
        verdict = verify_all([unknown], TODAY, sources={doc.name: doc.text})[0]
        self.assertEqual(verdict.findings, [])

    def test_auto_without_a_key_keeps_the_rules_result_and_says_why(self):
        doc = fixture_doc("glasswing_email_invoice.txt")
        inv = extract_document(doc, "auto", api_key=None, model=None)
        self.assertEqual(inv.layout, "unrecognised")
        self.assertIn(f"AI fallback skipped because {extract_ai.ENV_KEY} is not set", inv.warnings)

    def test_auto_keeps_the_rules_result_when_the_connection_times_out(self):
        doc = fixture_doc("glasswing_email_invoice.txt")
        opener = FakeOpener(*(TimeoutError("The read operation timed out") for _ in range(3)))
        inv = extract_document(doc, "auto", api_key=FAKE_KEY, model=None, opener=opener, sleep=no_sleep)
        self.assertEqual(inv.extractor, "rules")
        self.assertTrue(any(w.startswith("AI fallback failed: could not reach") for w in inv.warnings))

    def test_auto_keeps_the_rules_result_when_ai_fails(self):
        doc = fixture_doc("glasswing_email_invoice.txt")
        opener = FakeOpener(fixture_json("messages_response_refusal.json"))
        inv = extract_document(doc, "auto", api_key=FAKE_KEY, model=None, opener=opener, sleep=no_sleep)
        self.assertEqual(inv.extractor, "rules")
        self.assertTrue(any(w.startswith("AI fallback failed") for w in inv.warnings))

    def test_ai_mode_failure_becomes_a_review_item(self):
        opener = FakeOpener(fixture_json("messages_response_max_tokens.json"))
        inv = extract_document(sample("kestrel_KR-2291.txt"), "ai", api_key=FAKE_KEY, model=None,
                               opener=opener, sleep=no_sleep)
        verdict = verify_all([inv], TODAY)[0]
        self.assertEqual(verdict.status, "NEEDS REVIEW")
        self.assertIn("AI extraction failed", verdict.findings[0].message)


if __name__ == "__main__":
    unittest.main()
