"""Provider port for P5 execution (ADR-0012 §6) and its PayPal Sandbox implementation.

Every call returns a `ProviderResult` with an explicit `Outcome` instead of raising, so the
caller can record exactly what is known:

| Outcome      | Meaning                                                            |
|--------------|--------------------------------------------------------------------|
| OK           | 2xx                                                                |
| NOT_FOUND    | 404                                                                |
| REJECTED     | 400/409/422: the provider refused the request (business/validation)|
| FORBIDDEN    | 403: an access boundary; recorded, never worked around             |
| RETRYABLE    | the request was certainly not processed (connect failure, 429, 401 |
|              | after one token refresh), or it was a read that failed             |
| UNKNOWN      | a mutating request may have been processed: read/write timeout,    |
|              | dropped connection, 5xx. Look the resource up; never blind-retry   |

Nothing here decides financial state. Bodies are returned for the caller's strict checks;
they are never logged. `PayPal-Request-Id` is sent, but Elide's correctness does not rely
on it: PayPal does not document it for create/send invoice (ADR-0012 §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx

from api.integrations.paypal.client import PayPalSandboxClient, ProviderResponse
from api.integrations.paypal.redaction import fingerprint

SANDBOX = "sandbox"


class Outcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    FORBIDDEN = "forbidden"
    RETRYABLE = "retryable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    operation: str
    outcome: Outcome
    http_status: int | None
    body: Any
    debug_id: str | None = None
    error_name: str | None = None  # PayPal `name`, or the transport exception class

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


class PaymentProvider(Protocol):
    """What the execution layer may ask of a payment provider. No pay/capture/refund."""

    environment: str

    def credential_fingerprint(self) -> str: ...
    def create_invoice(self, payload: dict[str, Any], request_key: str) -> ProviderResult: ...
    def send_invoice(self, invoice_id: str, request_key: str) -> ProviderResult: ...
    def cancel_invoice(self, invoice_id: str, request_key: str) -> ProviderResult: ...
    def get_invoice(self, invoice_id: str) -> ProviderResult: ...
    def search_invoices(self, invoice_number: str) -> ProviderResult: ...
    def get_capture(self, capture_id: str) -> ProviderResult: ...
    def get_refund(self, refund_id: str) -> ProviderResult: ...


MUTATING = frozenset({"create_invoice", "send_invoice", "cancel_invoice"})


def classify_status(operation: str, status: int) -> Outcome:
    if 200 <= status < 300:
        return Outcome.OK
    if status == 404:
        return Outcome.NOT_FOUND
    if status == 403:
        return Outcome.FORBIDDEN
    if status in (400, 409, 422):
        return Outcome.REJECTED
    if status in (401, 429):
        return Outcome.RETRYABLE  # not processed
    if status >= 500:
        return Outcome.UNKNOWN if operation in MUTATING else Outcome.RETRYABLE
    return Outcome.UNKNOWN if operation in MUTATING else Outcome.RETRYABLE


def classify_exception(operation: str, exc: Exception) -> Outcome:
    # The request never left (or never reached) PayPal: safe to retry.
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return Outcome.RETRYABLE
    # Anything after the request may have been sent: unknown for a mutation.
    return Outcome.UNKNOWN if operation in MUTATING else Outcome.RETRYABLE


def _from_response(r: ProviderResponse) -> ProviderResult:
    body = r.body
    name = body.get("name") if isinstance(body, dict) and r.status_code >= 400 else None
    return ProviderResult(
        r.operation,
        classify_status(r.operation, r.status_code),
        r.status_code,
        body,
        r.debug_id,
        name,
    )


class PayPalSandboxProvider:
    """`PaymentProvider` over the Sandbox-only REST client (it refuses non-Sandbox URLs)."""

    environment = SANDBOX

    def __init__(self, client: PayPalSandboxClient):
        self._c = client

    def credential_fingerprint(self) -> str:
        return fingerprint(self._c.client_id)

    def _run(self, operation: str, fn, *args) -> ProviderResult:
        try:
            res = _from_response(fn(*args))
            if res.http_status == 401:
                # One token refresh: a 401 means the request was not processed.
                self._c.invalidate_token()
                res = _from_response(fn(*args))
            return res
        except httpx.HTTPError as exc:
            return ProviderResult(
                operation, classify_exception(operation, exc), None, None, None, type(exc).__name__
            )

    def create_invoice(self, payload: dict[str, Any], request_key: str) -> ProviderResult:
        return self._run("create_invoice", self._c.create_draft_invoice, payload, request_key)

    def send_invoice(self, invoice_id: str, request_key: str) -> ProviderResult:
        return self._run("send_invoice", self._c.send_invoice, invoice_id, request_key)

    def cancel_invoice(self, invoice_id: str, request_key: str) -> ProviderResult:
        return self._run("cancel_invoice", self._c.cancel_invoice, invoice_id, request_key)

    def get_invoice(self, invoice_id: str) -> ProviderResult:
        return self._run("get_invoice", self._c.get_invoice, invoice_id)

    def search_invoices(self, invoice_number: str) -> ProviderResult:
        return self._run("search_invoices", self._c.search_invoices, invoice_number)

    def get_capture(self, capture_id: str) -> ProviderResult:
        return self._run("get_capture", self._c.get_capture, capture_id)

    def get_refund(self, refund_id: str) -> ProviderResult:
        return self._run("get_refund", self._c.get_refund, refund_id)
