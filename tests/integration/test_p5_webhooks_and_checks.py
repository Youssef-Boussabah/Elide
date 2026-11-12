"""P5 webhooks over HTTP and the independent reconciliation checker (ADR-0012 A6, A8).

Signature verification is replaced by a scripted verifier here (PayPal's postback is unit-
tested against its documented contract); everything after it is the real intake, inbox,
outbox, worker and booking on PostgreSQL. **Synthetic-tested.**
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select, text

from api.accounting.reconciliation_check import CaptureExpectation, check_capture
from api.accounting.tables import provider_event_inbox
from api.execution.tables import provider_settlement

pytestmark = pytest.mark.integration


class ScriptedVerifier:
    configured = True

    def __init__(self):
        self.accept = True
        self.down = False

    def verify(self, headers, body):
        if self.down:
            raise RuntimeError("verification service unreachable")
        return self.accept


HEADERS = {
    "paypal-transmission-id": "t-1",
    "paypal-transmission-time": "2026-11-11T18:00:00Z",
    "paypal-transmission-sig": "sig",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/C",
    "paypal-auth-algo": "SHA256withRSA",
}


def _event(event_id, event_type, resource):
    return json.dumps({"id": event_id, "event_type": event_type, "resource": resource}).encode()


def _post(p, body):
    from fastapi.testclient import TestClient

    return TestClient(p.app).post(
        "/v1/provider/paypal/webhook",
        content=body,
        headers={**HEADERS, "content-type": "application/json"},
    )


def _sent(p):
    p.bind()
    _, intent = p.agree_and_approve()
    p.work()
    iid = intent["intent_id"]
    return iid, p.invoice_id(iid)


def _count(p, table):
    with p.engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar()


def test_verified_paid_webhook_triggers_a_get_and_one_booking(pilot):
    p = pilot
    v = ScriptedVerifier()
    p.app.state.webhook_verifier = v
    iid, inv = _sent(p)
    cap = p.mock.pay(inv)
    # The body claims a different amount: it must be irrelevant (only the GET counts).
    lie = {"id": inv, "status": "PAID", "amount": {"currency_code": "USD", "value": "9999.00"}}
    r = _post(p, _event("WH-PAID-0001", "INVOICING.INVOICE.PAID", lie))
    assert r.status_code == 202, r.text
    assert _post(p, _event("WH-PAID-0001", "INVOICING.INVOICE.PAID", lie)).json()["status"] == (
        "duplicate"
    )
    # A capture event for the same payment, delivered after (reordered/duplicated facts).
    cap_ev = {"id": cap, "invoice_id": inv, "status": "COMPLETED"}
    assert _post(p, _event("WH-CAP-0001", "PAYMENT.CAPTURE.COMPLETED", cap_ev)).status_code == 202
    p.work()
    assert p.intent(iid)["status"] == "reconciled"
    assert _count(p, provider_settlement) == 1
    with p.engine.connect() as c:
        assert c.execute(select(provider_settlement.c.gross_cents)).scalar() == 2400
        assert c.execute(select(func.count()).select_from(provider_event_inbox)).scalar() == 2


def test_forged_changed_and_unverifiable_events_change_nothing(pilot):
    p = pilot
    v = ScriptedVerifier()
    p.app.state.webhook_verifier = v
    iid, inv = _sent(p)
    v.accept = False
    assert _post(p, _event("WH-FORGED-01", "INVOICING.INVOICE.PAID", {"id": inv})).status_code == (
        400
    )
    v.accept, v.down = True, True
    r = _post(p, _event("WH-DOWN-0001", "INVOICING.INVOICE.PAID", {"id": inv}))
    assert r.status_code == 503 and r.json()["code"] == "VERIFIER_UNAVAILABLE"
    v.down = False
    assert _post(
        p, _event("WH-SAME-0001", "INVOICING.INVOICE.UPDATED", {"id": inv})
    ).status_code == (202)
    changed = _event("WH-SAME-0001", "INVOICING.INVOICE.PAID", {"id": inv})
    assert _post(p, changed).status_code == 409
    assert _count(p, provider_event_inbox) == 1
    p.work()
    # The verified UPDATED event only caused a GET: the invoice is unpaid, nothing changed.
    assert p.intent(iid)["status"] == "payer_action_pending" and _count(p, provider_settlement) == 0


def test_webhook_before_the_payment_exists_is_harmless(pilot):
    p = pilot
    p.app.state.webhook_verifier = ScriptedVerifier()
    iid, inv = _sent(p)
    assert (
        _post(p, _event("WH-EARLY-001", "INVOICING.INVOICE.PAID", {"id": inv})).status_code == 202
    )
    p.work()
    assert p.intent(iid)["status"] == "payer_action_pending"
    p.mock.pay(inv)
    p.work()
    assert p.intent(iid)["status"] == "reconciled" and _count(p, provider_settlement) == 1


def test_independent_checker_detects_tampered_books(pilot):
    """A hand-written, balanced but wrong entry in the payee's books is caught."""
    from api.accounting.journal import EntryKind, EvidenceClass, JournalEntry, Posting
    from api.accounting.store import post_entry
    from api.domain.money import USD, Money, SignedAmount

    p = pilot
    _iid, inv = _sent(p)
    cap = p.mock.pay(inv)
    p.work()
    x = CaptureExpectation(cap, inv, "pilot_seller", "pilot_buyer", "pilot_001", 2500, 2400, 100)
    with p.engine.connect() as c:
        assert check_capture(c, x) == []
    m = Money(100, USD)
    bad = JournalEntry(
        entity_id="pilot_seller",
        idempotency_key="test:tamper:receivable",
        kind=EntryKind.SYNTHETIC_TRANSFER,
        evidence_class=EvidenceClass.SYNTHETIC,
        description="tamper: re-open part of the receivable",
        occurred_at=p.now,
        postings=(
            Posting(f"accounts_receivable:{inv}", SignedAmount.debit(m)),
            Posting("revenue:sales", SignedAmount.credit(m)),
        ),
    )
    post_entry(p.engine, bad)
    with p.engine.connect() as c:
        problems = check_capture(c, x)
    assert any("receivable not cleared" in s for s in problems)
    # And a claim of a different cash amount is caught against the stored rows.
    with p.engine.connect() as c:
        wrong = CaptureExpectation(
            cap, inv, "pilot_seller", "pilot_buyer", "pilot_001", 2500, 2300, 100
        )
        assert check_capture(c, wrong)


def test_payer_link_refuses_when_the_provider_copy_differs(pilot):
    p = pilot
    iid, inv = _sent(p)
    p.mock.tamper(inv, amount={**p.mock.invoices[inv]["amount"], "value": "2400.00"})
    buyer = p.actor(p.buyer_login)
    buyer.reauth()
    r = buyer.get(f"/v1/m/pilot_buyer/payment-intents/{iid}/payer-link")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PROVIDER_TERMS_MISMATCH"
    assert "sandbox.paypal.com" not in r.text


def test_no_secret_or_payer_link_reaches_logs_tables_or_responses(pilot):
    from tests.integration.conftest import PILOT_PAYER_EMAIL

    p = pilot
    iid, inv = _sent(p)
    p.mock.pay(inv)
    p.work()
    seller = p.actor(p.seller_login)
    responses = [
        seller.get(f"/v1/m/pilot_seller/payment-intents/{iid}").text,
        seller.get("/v1/m/pilot_seller/settlements").text,
        p.actor(p.buyer_login).get(f"/v1/m/pilot_buyer/payment-intents/{iid}").text,
    ]
    with p.engine.connect() as c:
        tables = [
            t
            for (t,) in c.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
        ]
        dump = "".join(
            # Table names come from information_schema of this throwaway test database.
            str(c.execute(text(f"SELECT coalesce(json_agg(t)::text, '') FROM \"{t}\" t")).scalar())  # noqa: S608
            for t in tables
        )
    for blob in [dump, *responses]:
        assert "sandbox.paypal.com/invoice" not in blob
        assert PILOT_PAYER_EMAIL not in blob
        assert p.mock.client_id not in blob
