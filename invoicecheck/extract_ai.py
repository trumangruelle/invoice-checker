"""Optional AI extraction through the Anthropic Messages API, using only urllib.

Optional and off by default: the rules extractor needs no key and no network.
This step runs on the user's own Anthropic API key, only with --extractor ai
(every file) or --extractor auto (only files the rules extractor could not
fully read), and only when ANTHROPIC_API_KEY is set in the environment. The
key is read at run time and is never written to disk, logged, or put in the
request body. It is tested against fixture responses, not the live API.

The model is asked to transcribe, not to correct: the verification layer
needs the numbers as printed, including wrong ones. Its output is parsed into
the same Invoice schema as the rules extractor and goes through the same
checks, plus one more: every amount it returns must be printed in the source.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Callable

from .documents import Document
from .model import Invoice, LineItem
from .textutil import clean, parse_date, parse_money, parse_quantity

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ENV_KEY = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "low"          # transcription is not a reasoning task; the checks are the safety net
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 120
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

# Server-side refusal fallback ("default" routing), for the models that support it.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})

SYSTEM_PROMPT = (
    "You transcribe invoices into JSON for a bookkeeping program.\n"
    "Copy every value exactly as it is printed. Do not correct arithmetic, recompute "
    "totals or fill in values that are not printed: a separate program checks the "
    "arithmetic and needs the printed numbers, including wrong ones.\n"
    "Use null for anything that is not on the invoice. Write dates as YYYY-MM-DD; read "
    "slash dates as month/day/year. Write amounts, quantities and the tax rate as plain "
    "decimal strings without currency symbols or thousands separators, e.g. \"1240.00\"."
)


def _nullable(json_type: str) -> dict:
    return {"anyOf": [{"type": json_type}, {"type": "null"}]}


LINE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "quantity": _nullable("string"),
        "unit_price": _nullable("string"),
        "amount": _nullable("string"),
    },
    "required": ["description", "quantity", "unit_price", "amount"],
    "additionalProperties": False,
}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": _nullable("string"),
        "invoice_number": _nullable("string"),
        "invoice_date": _nullable("string"),
        "due_date": _nullable("string"),
        "currency": _nullable("string"),
        "line_items": {"type": "array", "items": LINE_ITEM_SCHEMA},
        "subtotal": _nullable("string"),
        "tax": _nullable("string"),
        "tax_rate_percent": _nullable("string"),
        "total": _nullable("string"),
    },
    "required": ["vendor", "invoice_number", "invoice_date", "due_date", "currency",
                 "line_items", "subtotal", "tax", "tax_rate_percent", "total"],
    "additionalProperties": False,
}


class AIExtractionError(Exception):
    """The AI path could not produce an invoice. The message never contains the key."""


def build_request_body(invoice_text: str, source_name: str, model: str = DEFAULT_MODEL,
                       effort: str = DEFAULT_EFFORT) -> dict:
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": INVOICE_SCHEMA},
        },
        "messages": [{
            "role": "user",
            "content": f"Invoice file: {source_name}\n\n<invoice>\n{invoice_text}\n</invoice>",
        }],
    }
    if model in FALLBACK_MODELS:
        body["fallbacks"] = "default"
    return body


def build_request(body: dict, api_key: str) -> urllib.request.Request:
    if not api_key:
        raise AIExtractionError(f"{ENV_KEY} is not set")
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if "fallbacks" in body:
        headers["anthropic-beta"] = FALLBACK_BETA
    return urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )


def _error_detail(err: urllib.error.HTTPError) -> str:
    try:
        raw = err.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - the body is optional
        return err.reason or ""
    try:
        data = json.loads(raw)
        error = data.get("error", {})
        return f"{error.get('type', 'error')}: {error.get('message', '')}".strip()
    except (ValueError, AttributeError):
        return raw[:300]


def _retry_delay(err: urllib.error.HTTPError | None, attempt: int) -> float:
    if err is not None:
        value = err.headers.get("retry-after") if err.headers else None
        if value:
            try:
                return min(float(value), 30.0)
            except ValueError:
                pass
    return min(2.0 ** attempt, 30.0)


def call_messages_api(body: dict, api_key: str, *, opener: Callable | None = None,
                      sleep: Callable[[float], None] = time.sleep, max_retries: int = 2,
                      timeout: float = TIMEOUT_SECONDS) -> dict:
    """POST to /v1/messages; retries 429/5xx/529, timeouts and connection errors, then gives up.

    Every failure leaves as AIExtractionError, so one bad call becomes a review
    item for that file instead of stopping the whole run.
    """
    opener = opener or urllib.request.urlopen
    attempt = 0
    while True:
        request = build_request(body, api_key)
        try:
            with opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            with err:  # an HTTPError holds the open response; close it either way
                if err.code in RETRY_STATUSES and attempt < max_retries:
                    delay = _retry_delay(err, attempt)
                else:
                    raise AIExtractionError(
                        f"Messages API returned HTTP {err.code} ({_error_detail(err)})"
                    ) from None
            sleep(delay)
            attempt += 1
            continue
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            # URLError covers a failure to connect or send. A timeout or dropped connection
            # while waiting for the response arrives unwrapped, as TimeoutError,
            # ConnectionResetError or an http.client exception (RemoteDisconnected,
            # IncompleteRead). HTTPError is a URLError too, but it is caught above.
            if attempt < max_retries:
                sleep(_retry_delay(None, attempt))
                attempt += 1
                continue
            detail = getattr(err, "reason", None) or err
            raise AIExtractionError(
                f"could not reach the Messages API ({type(err).__name__}: {detail})"
            ) from None
        except ValueError as err:
            raise AIExtractionError(f"Messages API response was not JSON: {err}") from None


def invoice_from_ai_fields(data: dict, source_file: str, model: str = "") -> Invoice:
    """Map the model's JSON onto the shared Invoice schema; unreadable values become warnings."""
    if not isinstance(data, dict):
        raise AIExtractionError("model output is not a JSON object")
    inv = Invoice(source_file=source_file, layout="ai", extractor=f"ai:{model}" if model else "ai")

    def text(name):
        value = data.get(name)
        return clean(value) if isinstance(value, str) and clean(value) else None

    def money(value, where):
        if value is None:
            return None
        parsed = parse_money(value) if isinstance(value, str) else None
        if parsed is None:
            inv.warnings.append(f"AI returned an unreadable {where}: {value!r}")
        return parsed

    inv.vendor = text("vendor")
    inv.invoice_number = text("invoice_number")
    inv.currency = text("currency") or "USD"
    for name in ("invoice_date", "due_date"):
        raw = text(name)
        if raw:
            setattr(inv, f"{name}_raw", raw)
            setattr(inv, name, parse_date(raw))
    inv.subtotal = money(data.get("subtotal"), "subtotal")
    inv.tax = money(data.get("tax"), "tax")
    inv.total = money(data.get("total"), "total")
    rate = data.get("tax_rate_percent")
    if isinstance(rate, str) and rate.strip():
        inv.tax_rate = parse_quantity(rate.strip().rstrip("%").strip())
        if inv.tax_rate is None:
            inv.warnings.append(f"AI returned an unreadable tax rate: {rate!r}")

    items = data.get("line_items") or []
    if not isinstance(items, list):
        raise AIExtractionError("model output line_items is not a list")
    for n, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            inv.warnings.append(f"AI returned a line item that is not an object (item {n})")
            continue
        qty_raw = item.get("quantity")
        qty = parse_quantity(qty_raw) if isinstance(qty_raw, str) else None
        if qty_raw is not None and qty is None:
            inv.warnings.append(f"AI returned an unreadable quantity on item {n}: {qty_raw!r}")
        inv.line_items.append(LineItem(
            description=clean(item.get("description") or ""),
            quantity=qty,
            unit_price=money(item.get("unit_price"), f"unit price on item {n}"),
            amount=money(item.get("amount"), f"amount on item {n}"),
            source_line=n,
        ))
    return inv


def parse_response(payload: dict, source_file: str) -> Invoice:
    """Turn a Messages API response into an Invoice, or raise AIExtractionError."""
    stop = payload.get("stop_reason")
    if stop == "refusal":
        raise AIExtractionError("the model declined to process this document (stop_reason=refusal)")
    if stop == "max_tokens":
        raise AIExtractionError("the response hit max_tokens, so the JSON is incomplete")
    blocks = payload.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise AIExtractionError("the response contained no text block")
    try:
        data = json.loads(text)
    except ValueError as err:
        raise AIExtractionError(f"the model's output was not valid JSON: {err}") from None
    return invoice_from_ai_fields(data, source_file, payload.get("model", ""))


def extract_with_ai(doc: Document, api_key: str, *, model: str = DEFAULT_MODEL,
                    effort: str = DEFAULT_EFFORT, opener: Callable | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> Invoice:
    body = build_request_body(doc.text, doc.name, model=model, effort=effort)
    payload = call_messages_api(body, api_key, opener=opener, sleep=sleep)
    return parse_response(payload, doc.name)
