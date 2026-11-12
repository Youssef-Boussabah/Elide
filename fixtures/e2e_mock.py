"""Constants and bindings for browser tests against the MOCK PayPal provider (P5 gate review).

These are fake test values shared by `scripts/e2e_setup.py` (which binds the pilot
merchants to them) and `scripts/e2e_app.py` (which configures the in-process mock with
them). They identify nothing real; no PayPal account is involved.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine

from api.execution import bindings
from api.integrations.paypal.redaction import fingerprint

E2E_CLIENT_ID = "e2e-mock-client-id"
E2E_PAYEE_MERCHANT_ID = "E2EMOCKPAYEE01"
E2E_PAYER_EMAIL = "e2e-payer@mock.example.test"
E2E_WRONG_PAYEE = "E2EMOCKSOMEONEELSE"


def bind_pilot(engine: Engine, at: datetime) -> None:
    with engine.begin() as conn:
        bindings.bind(
            conn,
            merchant_id="pilot_seller",
            role="invoicer",
            credential_fingerprint=fingerprint(E2E_CLIENT_ID),
            account_fingerprint=fingerprint(E2E_PAYEE_MERCHANT_ID),
            consent_note="E2E TEST: mock provider only",
            bound_by="e2e-setup",
            at=at,
            settlement_currency="USD",
        )
        bindings.bind(
            conn,
            merchant_id="pilot_buyer",
            role="payer",
            credential_fingerprint=fingerprint(E2E_PAYER_EMAIL),
            account_fingerprint=fingerprint(E2E_PAYER_EMAIL),
            consent_note="E2E TEST: mock provider only",
            bound_by="e2e-setup",
            at=at,
        )
