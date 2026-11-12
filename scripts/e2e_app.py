"""Browser-test harness app: the real Elide API + an in-process MOCK PayPal (P5 gate review).

    ELIDE_E2E_HARNESS=1 uv run uvicorn --app-dir scripts e2e_app:app --host 127.0.0.1

Only for Playwright, only with APP_ENV local/test/ci and `ELIDE_E2E_HARNESS=1`, and every
`/__e2e__` route answers loopback clients only. It is not part of the product: nothing in
`api/` imports it, and the production app has no such routes. No real PayPal call can
happen: the provider is `MockPayPal`.

Test-only controls (they stand in for things outside Elide):
- `POST /__e2e__/worker/run`  : run the outbox worker until idle (what `run_worker.py`
  does), making already-queued messages due now;
- `POST /__e2e__/mock/pay`    : the payer pays the newest intent of an obligation in
  "PayPal's hosted page" (optionally as a different payee account, to test the guard);
- `GET  /__e2e__/mock/calls`  : the provider calls made so far (operations and ids only).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import APIRouter, HTTPException, Request  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from api.app.main import create_app  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.db import get_engine  # noqa: E402
from api.execution.service import Runtime, handlers_for, on_dead  # noqa: E402
from api.execution.tables import payment_session  # noqa: E402
from api.integrations.paypal.mock import build_mock_provider  # noqa: E402
from api.workers.outbox import RetryPolicy, run_once  # noqa: E402
from api.workflow.tables import outbox_message, payment_intent  # noqa: E402
from fixtures.e2e_mock import E2E_CLIENT_ID, E2E_PAYEE_MERCHANT_ID, E2E_PAYER_EMAIL  # noqa: E402

settings = get_settings()
if not (settings.demo_identities_allowed and os.environ.get("ELIDE_E2E_HARNESS") == "1"):
    raise RuntimeError("the e2e harness app runs only for browser tests (local/test/ci)")

app = create_app()
mock = build_mock_provider(
    settings.app_env, client_id=E2E_CLIENT_ID, payee_merchant_id=E2E_PAYEE_MERCHANT_ID
)
app.state.payment_provider = mock
app.state.payer_email = lambda: E2E_PAYER_EMAIL
app.state.payments_execution_enabled = True

router = APIRouter(prefix="/__e2e__", include_in_schema=False)


def _loopback(request: Request) -> None:
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(404)


def _now() -> datetime:
    return datetime.now(UTC)


@router.post("/worker/run")
def run_worker(request: Request) -> dict:
    _loopback(request)
    engine = get_engine()
    rt = Runtime(
        engine=engine,
        provider=mock,
        payer_email=lambda: E2E_PAYER_EMAIL,
        execution_enabled=lambda: True,
        clock=_now,
        worker_id="e2e-worker",
        auto_cancel=False,
    )
    results = []
    with engine.begin() as conn:  # queued work becomes due now, once (a test does not wait)
        conn.execute(
            update(outbox_message)
            .where(outbox_message.c.status == "pending")
            .values(available_at=_now())
        )
    for _ in range(12):
        done = run_once(
            engine,
            handlers_for(rt),
            worker_id=rt.worker_id,
            clock=_now,
            policy=RetryPolicy(base_s=0.01, cap_s=0.01),
            on_dead=lambda j, e: on_dead(rt, j, e),
        )
        results += [{"kind": j.kind, "action": r.action, "error": r.error} for j, r in done]
        if not done:
            break
    return {"results": results}


class PayBody(BaseModel):
    obligation_id: str
    payee_merchant_id: str | None = None


@router.post("/mock/pay")
def pay(body: PayBody, request: Request) -> dict:
    _loopback(request)
    with get_engine().connect() as conn:
        inv = conn.execute(
            select(payment_session.c.provider_invoice_id)
            .join(payment_intent, payment_intent.c.intent_id == payment_session.c.intent_id)
            .where(payment_intent.c.obligation_id == body.obligation_id)
            .order_by(payment_intent.c.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "no PayPal invoice for that obligation yet")
    capture = mock.pay(inv, payee_merchant_id=body.payee_merchant_id)
    return {"paid": True, "capture_id": capture}


@router.get("/mock/calls")
def calls(request: Request) -> dict:
    _loopback(request)
    return {"calls": [{"operation": op, "args": list(args)} for op, args in mock.calls]}


app.include_router(router)
