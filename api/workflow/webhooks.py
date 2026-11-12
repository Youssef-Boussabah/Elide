"""PayPal webhook intake (ADR-0011 §7): validate, verify, deduplicate, enqueue a fetch.

A webhook body is never financial truth. An accepted event is recorded once in the P1
`provider_event_inbox` (provider event id + environment; a redelivery with a different
payload is a conflict) and enqueues a HELD `provider_fetch` outbox message: P5 fetches
the provider resource with a GET before any state changes. Without a configured
signature verifier the endpoint refuses everything (503) and records nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.accounting.store import (
    InboxConflictError,
    InboxStatus,
    payload_digest,
    record_provider_event,
)
from api.workflow import audit
from api.workflow.tables import outbox_message

REQUIRED_HEADERS = (
    "paypal-transmission-id",
    "paypal-transmission-time",
    "paypal-transmission-sig",
    "paypal-cert-url",
    "paypal-auth-algo",
)
MAX_BODY = 64 * 1024
_EVENT_ID = re.compile(r"^WH-[A-Za-z0-9-]{4,100}$")
_EVENT_TYPE = re.compile(r"^[A-Z][A-Z0-9_.]{2,100}$")


class WebhookVerifier(Protocol):
    configured: bool

    def verify(self, headers: dict[str, str], body: bytes) -> bool: ...


class RefusingVerifier:
    """Default: no verifier configured, so nothing is accepted (P5 adds PayPal's
    verify-webhook-signature call with the configured webhook id)."""

    configured = False

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    status: int
    body: dict


def receive(
    engine: Engine,
    verifier: WebhookVerifier,
    headers: dict[str, str],
    body: bytes,
    *,
    environment: str,
    now: datetime,
) -> WebhookOutcome:
    if len(body) > MAX_BODY:
        return WebhookOutcome(413, {"code": "TOO_LARGE"})
    h = {k.lower(): v for k, v in headers.items()}
    missing = [k for k in REQUIRED_HEADERS if not h.get(k)]
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        event = None
    ok_shape = (
        isinstance(event, dict)
        and isinstance(event.get("id"), str)
        and _EVENT_ID.match(event["id"])
        and isinstance(event.get("event_type"), str)
        and _EVENT_TYPE.match(event["event_type"])
        and isinstance(event.get("resource"), dict)
    )
    if missing or not ok_shape:
        audit.append_separately(
            engine,
            at=now,
            action="webhook.rejected",
            object_type="webhook",
            object_id="malformed",
            outcome="denied",
            detail={"reason": "MALFORMED", "missing_headers": missing},
        )
        return WebhookOutcome(422, {"code": "MALFORMED"})
    if not verifier.configured:
        return WebhookOutcome(503, {"code": "VERIFIER_NOT_CONFIGURED"})
    if not verifier.verify(h, body):
        audit.append_separately(
            engine,
            at=now,
            action="webhook.rejected",
            object_type="webhook",
            object_id=event["id"],
            outcome="denied",
            detail={"reason": "SIGNATURE_INVALID", "event_type": event["event_type"]},
        )
        return WebhookOutcome(400, {"code": "SIGNATURE_INVALID"})
    try:
        res = record_provider_event(
            engine,
            provider="paypal",
            environment=environment,
            provider_event_id=event["id"],
            event_type=event["event_type"],
            payload=body,
        )
    except InboxConflictError:
        audit.append_separately(
            engine,
            at=now,
            action="webhook.rejected",
            object_type="webhook",
            object_id=event["id"],
            outcome="conflict",
            detail={"reason": "PAYLOAD_CHANGED"},
        )
        return WebhookOutcome(409, {"code": "PAYLOAD_CHANGED"})
    if res.status is InboxStatus.DUPLICATE:
        return WebhookOutcome(200, {"status": "duplicate", "inbox_id": res.inbox_id})
    payload = {"provider_event_id": event["id"], "resource_type": event["event_type"]}
    with engine.begin() as conn:
        conn.execute(
            pg_insert(outbox_message)
            .values(
                dedupe_key=f"fetch:{environment}:{event['id']}",
                kind="provider_fetch",
                payload=payload,
                payload_sha256=payload_digest(payload),
                status="held",
                available_at=now,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
        audit.append(
            conn,
            at=now,
            action="webhook.accepted",
            object_type="webhook",
            object_id=event["id"],
            detail={"event_type": event["event_type"], "inbox_id": res.inbox_id},
        )
    return WebhookOutcome(
        202,
        {
            "status": "accepted",
            "inbox_id": res.inbox_id,
            "note": "recorded once; provider state is fetched by GET before anything changes",
        },
    )
