"""Append-only, hash-chained audit trail (ADR-0011 §5).

Each event's SHA-256 covers the previous event's hash and its own canonical content, so
any edit, deletion or reordering breaks `verify_chain`. Appends are serialized with a
transaction-scoped advisory lock. The table also refuses UPDATE/DELETE by trigger.

`merchant_id` is the scope: merchant-scoped events are readable by that merchant's
auditors; NULL-scoped events form the security log (anonymous or denied access) and are
never returned to merchants. Callers pass only redacted detail (ids, codes, amounts in
cents) - never tokens, passwords, secrets or another merchant's private figures.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select, text

from api.workflow.tables import audit_event

GENESIS = "0" * 64
_LOCK_KEY = 0x454C494445  # "ELIDE"
_SECRET_SUBSTRINGS = ("password", "token", "csrf", "secret", "cookie", "api_key")
_SECRET_EXACT = {"authorization", "set-cookie"}


def _canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _content(
    *,
    occurred_at: datetime,
    actor_user_id: str | None,
    session_id: str | None,
    merchant_id: str | None,
    action: str,
    object_type: str,
    object_id: str,
    outcome: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "occurred_at": occurred_at.astimezone(UTC).isoformat(),
        "actor_user_id": actor_user_id,
        "session_id": session_id,
        "merchant_id": merchant_id,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "outcome": outcome,
        "detail": detail,
    }


def _check_detail(detail: dict[str, Any]) -> None:
    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k).lower()
                if key in _SECRET_EXACT or any(f in key for f in _SECRET_SUBSTRINGS):
                    raise ValueError(f"audit detail must not contain {k!r}")
                walk(v)
        elif isinstance(obj, list | tuple):
            for v in obj:
                walk(v)

    walk(detail)


def append(
    conn: Connection,
    *,
    at: datetime,
    action: str,
    object_type: str,
    object_id: str,
    outcome: str = "ok",
    actor_user_id: str | None = None,
    session_id: str | None = None,
    merchant_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    detail = detail or {}
    _check_detail(detail)
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = conn.execute(
        select(audit_event.c.event_sha256).order_by(audit_event.c.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev = prev or GENESIS
    content = _content(
        occurred_at=at,
        actor_user_id=actor_user_id,
        session_id=session_id,
        merchant_id=merchant_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        outcome=outcome,
        detail=json.loads(_canonical(detail)),
    )
    digest = hashlib.sha256((prev + _canonical(content)).encode()).hexdigest()
    conn.execute(
        audit_event.insert().values(
            occurred_at=at,
            actor_user_id=actor_user_id,
            session_id=session_id,
            merchant_id=merchant_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            outcome=outcome,
            detail=content["detail"],
            prev_sha256=prev,
            event_sha256=digest,
        )
    )
    return digest


def append_separately(engine: Engine, **kw: Any) -> str:
    """Record an event in its own transaction (denials survive the caller's rollback)."""
    with engine.begin() as conn:
        return append(conn, **kw)


def verify_chain(conn: Connection) -> list[str]:
    issues: list[str] = []
    prev = GENESIS
    for r in conn.execute(select(audit_event).order_by(audit_event.c.id)).mappings():
        if r["prev_sha256"] != prev:
            issues.append(f"event {r['id']}: previous hash does not match")
        content = _content(
            occurred_at=r["occurred_at"],
            actor_user_id=r["actor_user_id"],
            session_id=r["session_id"],
            merchant_id=r["merchant_id"],
            action=r["action"],
            object_type=r["object_type"],
            object_id=r["object_id"],
            outcome=r["outcome"],
            detail=r["detail"],
        )
        want = hashlib.sha256((r["prev_sha256"] + _canonical(content)).encode()).hexdigest()
        if want != r["event_sha256"]:
            issues.append(f"event {r['id']}: content hash does not match")
        prev = r["event_sha256"]
    return issues


def for_merchant(conn: Connection, merchant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(audit_event)
        .where(audit_event.c.merchant_id == merchant_id)
        .order_by(audit_event.c.id.desc())
        .limit(limit)
    ).mappings()
    return [
        {
            "id": r["id"],
            "occurred_at": r["occurred_at"].isoformat(),
            "actor_user_id": r["actor_user_id"],
            "action": r["action"],
            "object_type": r["object_type"],
            "object_id": r["object_id"],
            "outcome": r["outcome"],
            "detail": r["detail"],
            "event_sha256": r["event_sha256"],
        }
        for r in rows
    ]
