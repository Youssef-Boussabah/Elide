"""P5 execution and reconciliation end to end (PostgreSQL + mock PayPal; ADR-0012).

Everything here is **Synthetic-tested**: the provider is the in-memory double, never PayPal.
The flow is the product's own: two separate merchant sessions over HTTP, the payer's
approval, a non-synthetic intent, the outbox worker, the hosted payer link, a simulated
payer action at "PayPal", reconciliation by GET and the independent check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from api.accounting.reconciliation_check import CaptureExpectation, check_capture
from api.accounting.store import trial_balance
from api.execution.tables import (
    obligation_settlement_event,
    payment_state_event,
    provider_operation,
    provider_settlement,
)
from api.liquidity.tables import capacity_commitment
from api.workflow.audit import verify_chain
from api.workflow.tables import outbox_message

pytestmark = pytest.mark.integration


def _states(p, intent_id):
    with p.engine.connect() as c:
        return [
            r.to_status
            for r in c.execute(
                select(payment_state_event.c.to_status)
                .where(payment_state_event.c.intent_id == intent_id)
                .order_by(payment_state_event.c.id)
            )
        ]


def _commitments(p, intent_id):
    with p.engine.connect() as c:
        return {
            r.commitment_id: (
                r.kind,
                int(r.amount_cents),
                r.released_at is not None,
                r.release_reason,
            )
            for r in c.execute(
                select(capacity_commitment).where(
                    capacity_commitment.c.commitment_id.in_((f"pi:{intent_id}", f"so:{intent_id}"))
                )
            )
        }


def test_happy_path_books_once_and_keeps_outflow_committed(pilot):
    p = pilot
    p.bind()
    buyer, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    assert intent["status"] == "approved" and intent["status_reason"] == "QUEUED_FOR_PROVIDER"
    assert intent["amount_cents"] == 2400 and intent["evidence_kind"] == "merchant_attested"

    p.work()
    assert p.intent(iid)["status"] == "payer_action_pending"
    inv = p.mock.invoices[p.invoice_id(iid)]
    assert inv["status"] == "SENT" and inv["amount"]["value"] == "24.00"
    assert inv["amount"]["breakdown"]["discount"]["invoice_discount"]["amount"]["value"] == "-1.00"
    assert inv["configuration"]["partial_payment"]["allow_partial_payment"] is False
    assert p.mock.created_count() == 1

    # The payer opens PayPal's hosted page (fetched on demand; never stored).
    buyer.reauth()
    link = buyer.get(f"/v1/m/pilot_buyer/payment-intents/{iid}/payer-link")
    assert link.status_code == 200, link.text
    assert link.json()["url"].startswith("https://www.sandbox.paypal.com/")
    assert link.headers["cache-control"] == "no-store"
    with p.engine.connect() as c:
        dump = c.execute(text("SELECT row_to_json(t)::text FROM payment_session t")).scalars().all()
        audit_dump = c.execute(text("SELECT detail::text FROM audit_event")).scalars().all()
    assert not any("sandbox.paypal.com" in d for d in dump + audit_dump)

    # The payer pays at "PayPal" (simulated human action), then the worker reconciles.
    cap = p.mock.pay(p.invoice_id(iid))
    p.work()
    assert p.intent(iid)["status"] == "reconciled"
    assert _states(p, iid) == [
        "session_pending",
        "payer_action_pending",
        "provider_confirmed",
        "reconciled",
    ]
    with p.engine.connect() as c:
        st = c.execute(select(provider_settlement)).mappings().one()
        assert st["capture_id"] == cap and st["gross_cents"] == 2400
        assert st["net_cents"] == st["gross_cents"] - st["fee_cents"]
        assert (
            c.execute(select(func.count()).select_from(obligation_settlement_event)).scalar() == 1
        )
        assert not check_capture(
            c,
            CaptureExpectation(
                cap, st["invoice_id"], "pilot_seller", "pilot_buyer", "pilot_001", 2500, 2400, 100
            ),
        )
    assert all(v == 0 for v in trial_balance(p.engine).values())
    # The reservation became a settled outflow that stays committed (stale evidence).
    cm = _commitments(p, iid)
    assert cm[f"pi:{iid}"][2] is True and cm[f"pi:{iid}"][3].startswith("settled:capture:")
    assert cm[f"so:{iid}"] == ("settled_outflow", 2400, False, None)

    # Replays (webhook-triggered fetch, another reconcile) never post twice.
    with p.engine.begin() as c:
        from api.execution.service import schedule

        schedule(
            c,
            kind="provider_fetch",
            dedupe_key="fetch:test:dup",
            intent_id=None,
            payload={"invoice_id": p.invoice_id(iid)},
            at=p.now,
        )
    p.work()
    with p.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(provider_settlement)).scalar() == 1
        n = c.execute(
            text("SELECT count(*) FROM journal_entry WHERE provider_resource_id = :c"), {"c": cap}
        ).scalar()
        assert n == 2  # payee settlement + payer payment-sent
        assert verify_chain(c) == []

    # Views: the payee sees its receipt as provider-confirmed, NOT spendable; the payer
    # never sees the payee's fee; the payee never sees the payer's reservation.
    seller = p.actor(p.seller_login)
    sv = seller.get(f"/v1/m/pilot_seller/payment-intents/{iid}").json()
    assert sv["settlement"]["receipt_status"] == "PROVIDER_CONFIRMED_NOT_SPENDABLE"
    assert sv["settlement"]["usd_deployable_cents"] == 0 and sv["funds_usable"] is False
    assert "cash_reservation" not in sv and "fee_ceiling_cents" not in sv
    bv = buyer.get(f"/v1/m/pilot_buyer/payment-intents/{iid}").json()
    assert "fee_cents" not in bv["settlement"] and bv["reconciled"] is True

    # New attested evidence that does NOT list the outflow releases nothing ...
    p.advance(minutes=5)
    buyer.reauth()
    r = buyer.post(
        "/v1/m/pilot_buyer/capacity-evidence",
        {"verified_spendable_cents": 17600, "protected_reserve_cents": 5000},
        key="attest-key-1",
    )
    assert r.status_code == 200, r.text
    assert r.json()["released_outflows"] == []
    assert _commitments(p, iid)[f"so:{iid}"][2] is False
    # ... evidence that lists it releases exactly that commitment.
    p.advance(minutes=5)
    buyer.reauth()
    r = buyer.post(
        "/v1/m/pilot_buyer/capacity-evidence",
        {
            "verified_spendable_cents": 17600,
            "protected_reserve_cents": 5000,
            "included_outflow_ids": [f"so:{iid}"],
        },
        key="attest-key-2",
    )
    assert r.status_code == 200, r.text
    assert r.json()["released_outflows"] == [f"so:{iid}"]
    assert _commitments(p, iid)[f"so:{iid}"][3].startswith("reflected_in_evidence:")

    with p.engine.connect() as c:
        kinds = c.execute(
            select(provider_operation.c.operation, provider_operation.c.state).order_by(
                provider_operation.c.id
            )
        ).all()
        assert ("create_invoice", "succeeded") in kinds and ("send_invoice", "succeeded") in kinds
        assert not c.execute(
            select(outbox_message.c.id).where(outbox_message.c.status.in_(("leased", "dead")))
        ).first()
