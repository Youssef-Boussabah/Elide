"""`POST /v1/provider/paypal/webhook`: verified, deduplicated intake; no state change."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.app.auth.deps import engine_of, now_of
from api.workflow.webhooks import receive

router = APIRouter(tags=["provider webhooks"])


@router.post("/v1/provider/paypal/webhook")
async def paypal_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    out = receive(
        engine_of(request),
        request.app.state.webhook_verifier,
        dict(request.headers),
        body,
        environment="sandbox",
        now=now_of(request),
    )
    return JSONResponse(status_code=out.status, content=out.body)
