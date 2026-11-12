"""Client operation keys for mutations (doc 09 "Request integrity").

The same key with the same body returns the stored response; the same key with a
different body is a 409. Keys are scoped per merchant and serialized with an advisory
lock, so two concurrent requests with one key execute the operation once. Failed
operations roll back and are not stored, so a corrected retry may reuse the key.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, text

from api.workflow.store import ConflictError, InvalidRequestError
from api.workflow.tables import idempotency_record

_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")


def body_hash(route: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"route": route, "body": body}, sort_keys=True, default=str).encode()
    ).hexdigest()


def run(
    engine: Engine,
    *,
    merchant_id: str,
    user_id: str,
    key: str | None,
    route: str,
    body: dict[str, Any],
    at: datetime,
    operation: Callable[[Connection], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """(response, replayed)."""
    if not key or not _KEY.match(key):
        raise InvalidRequestError(
            "an Idempotency-Key header (8-100 characters: letters, digits, _ . : -) is required",
            code="IDEMPOTENCY_KEY_REQUIRED",
        )
    h = body_hash(route, body)
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 1))"),
            {"k": f"{merchant_id}|{key}"},
        )
        prior = (
            conn.execute(
                select(idempotency_record).where(
                    idempotency_record.c.merchant_id == merchant_id,
                    idempotency_record.c.operation_key == key,
                )
            )
            .mappings()
            .one_or_none()
        )
        if prior is not None:
            if prior["request_sha256"] != h:
                raise ConflictError(
                    "this operation key was already used for a different request",
                    code="IDEMPOTENCY_CONFLICT",
                )
            return prior["response"], True
        response = operation(conn)
        response = json.loads(json.dumps(response, default=str))
        conn.execute(
            insert(idempotency_record).values(
                merchant_id=merchant_id,
                operation_key=key,
                user_id=user_id,
                route=route,
                request_sha256=h,
                status_code=200,
                response=response,
                created_at=at,
            )
        )
        return response, False
