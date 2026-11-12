"""P5 failure injection, concurrency and adversarial cases (PostgreSQL + mock PayPal).

ADR-0012 acceptance rows A2-A10. **Synthetic-tested** only: the provider is the in-memory
double. Each test states the money rule it protects.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from api.execution.tables import (
    payment_exception,
    provider_operation,
    provider_settlement,
)
from api.liquidity.store import active_commitment_total
from api.liquidity.tables import capacity_commitment
from api.workers.outbox import Job, LeaseLostError, RetryPolicy, assert_lease, claim, finish
from api.workflow.tables import outbox_message, payment_intent

pytestmark = pytest.mark.integration

RESERVED = 2400  # amount (fee ceiling is 0 in the pilot fixture)


def _status(p, iid):
    return p.intent(iid)["status"]


def _ops(p, iid):
    with p.engine.connect() as c:
        return [
            (o, s)
            for o, s in c.execute(
                select(provider_operation.c.operation, provider_operation.c.state)
                .where(provider_operation.c.intent_id == iid)
                .order_by(provider_operation.c.id)
            )
        ]


def _exceptions(p, iid):
    with p.engine.connect() as c:
        return [
            r
            for (r,) in c.execute(
                select(payment_exception.c.code).where(payment_exception.c.intent_id == iid)
            )
        ]


def _released(p, iid):
    with p.engine.connect() as c:
        r = c.execute(
            select(capacity_commitment.c.released_at, capacity_commitment.c.release_reason).where(
                capacity_commitment.c.commitment_id == f"pi:{iid}"
            )
        ).one()
    return r.released_at is not None, r.release_reason


def _settlements(p):
    with p.engine.connect() as c:
        return c.execute(select(func.count()).select_from(provider_settlement)).scalar()


def _calls(p, op):
    return sum(1 for o, _ in p.mock.calls if o == op)


def _revoke_buyer(c, p) -> None:
    from api.app.auth import service as auth

    auth.revoke_membership(
        c,
        p.seeded.users[p.buyer_login],
        "pilot_buyer",
        at=p.now,
        revoked_by=None,
        reason="test: approver authority withdrawn",
    )


def _confirm_reserve(actor, reserve: str) -> None:
    text_ = f"Keep ${reserve} protected at all times. Only pay early for at least $1 off."
    extraction = {
        "reserve_floor": {
            "amount": reserve,
            "currency": "$",
            "quote": f"Keep ${reserve} protected",
        },
        "max_early_commitment": None,
        "min_early_discount": {"amount": "1", "currency": "$", "quote": "at least $1 off"},
        "min_net_benefit": None,
        "fee_ceiling": None,
        "allowed_counterparties": [],
        "prohibited_counterparties": [],
        "policy_valid_until": None,
        "offer_response_deadline": None,
        "participation": "unspecified",
        "approval_preference": "unspecified",
        "ambiguities": [],
        "refused_requests": [],
        "summary": "Reserve and minimum discount.",
    }
    d = actor.post("/v1/m/pilot_buyer/policy/drafts", {"text": text_, "extraction": extraction})
    assert d.status_code == 200 and d.json()["status"] == "READY_FOR_CONFIRMATION", d.text
    c = actor.post(
        f"/v1/m/pilot_buyer/policy/drafts/{d.json()['draft_id']}/confirm",
        {"digest": d.json()["digest"]},
    )
    assert c.status_code == 200, c.text


# --- holds and revalidation (A2) ------------------------------------------------------------
def test_unbound_payee_is_held_and_nothing_dispatches(pilot):
    p = pilot
    _, intent = p.agree_and_approve()
    assert intent["status_reason"] == "PROVIDER_NOT_CONNECTED"
    assert p.work() == [] and p.mock.calls == []


def test_kill_switch_holds_at_creation(pilot):
    p = pilot
    p.bind()
    p.app.state.payments_execution_enabled = False
    _, intent = p.agree_and_approve()
    assert intent["status_reason"] == "EXECUTION_DISABLED" and intent["execution"] == "disabled"
    assert p.work() == [] and p.mock.calls == []


def test_kill_switch_after_queueing_stops_mutations_but_not_reconciliation(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    p.flags["execution"] = False
    p.work()
    assert _status(p, intent["intent_id"]) == "approved" and p.mock.calls == []


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("wrong_credentials", "PAYEE_CREDENTIAL_MISMATCH"),
        ("revoke_approver", "AUTHORIZATION_REVOKED"),
        ("new_reserve_policy", "CONDITIONS_CHANGED"),
        ("expire_authorization", "AUTHORIZATION_EXPIRED"),
    ],
)
def test_revalidation_failure_before_any_provider_call_cancels_and_releases(pilot, tamper, code):
    p = pilot
    if tamper == "wrong_credentials":
        p.bind(invoicer_client_id="someone-elses-client-id")
    else:
        p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    if tamper == "revoke_approver":
        with p.engine.begin() as c:
            _revoke_buyer(c, p)
    if tamper == "new_reserve_policy":
        # A newer confirmed policy with a higher reserve, via the product's own routes.
        buyer = p.actor(p.buyer_login)
        buyer.reauth()
        _confirm_reserve(buyer, "70")
    if tamper == "expire_authorization":
        p.advance(days=2)
    p.work(rounds=3)
    assert _status(p, iid) == "cancelled"
    assert p.intent(iid)["status_reason"] == code
    assert _released(p, iid) == (True, f"cancelled:{code}")
    assert _calls(p, "create_invoice") == 0 and _calls(p, "send_invoice") == 0


# --- unknown outcomes and crash recovery (A4, A5) -------------------------------------------
def test_create_timeout_after_commit_is_looked_up_not_repeated(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    p.mock.inject("create_invoice", "after")
    p.work()
    assert _status(p, iid) == "payer_action_pending"
    assert p.mock.created_count() == 1 and _calls(p, "create_invoice") == 1
    assert ("create_invoice", "unknown") in _ops(p, iid)
    assert ("search_invoices", "succeeded") in _ops(p, iid)


def test_create_unknown_with_search_lag_retries_lookup_then_adopts(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    p.mock.inject("create_invoice", "5xx_after")
    p.mock.search_lag(2)
    p.work()
    assert _status(p, iid) == "payer_action_pending"
    assert p.mock.created_count() == 1 and _calls(p, "search_invoices") >= 2


def test_create_not_sent_is_retried(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    p.mock.inject("create_invoice", "before", "before")
    p.work()
    assert _status(p, intent["intent_id"]) == "payer_action_pending"
    assert p.mock.created_count() == 1


def test_create_rejected_fails_and_releases(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    p.mock.inject("create_invoice", "reject")
    p.work()
    assert _status(p, iid) == "failed" and _released(p, iid)[0] is True
    assert p.mock.created_count() == 0


@pytest.mark.parametrize("fault", ["after", "5xx_after"])
def test_send_unknown_is_confirmed_by_get(pilot, fault):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    p.mock.inject("send_invoice", fault)
    p.work()
    assert _status(p, iid) == "payer_action_pending"
    assert _calls(p, "send_invoice") == 1  # the GET showed SENT: no second send


def test_crash_between_call_and_record_is_recovered_as_unknown(pilot, monkeypatch):
    """The worker dies after PayPal created the draft but before recording the result."""
    import api.execution.service as svc

    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    real = svc._finish_op
    crashed = {"done": False}

    def crash_once(rt, op_id, res):
        if res.operation == "create_invoice" and not crashed["done"]:
            crashed["done"] = True
            raise SystemExit("worker killed")  # not caught by the handler guard
        return real(rt, op_id, res)

    monkeypatch.setattr(svc, "_finish_op", crash_once)
    with pytest.raises(SystemExit):
        p.work(rounds=1)
    monkeypatch.setattr(svc, "_finish_op", real)
    assert _ops(p, iid)[-1] == ("create_invoice", "started")
    # The lease and the stale in-flight call expire; another worker takes over.
    p.advance(minutes=10)
    p.work(worker_id="w2")
    assert _status(p, iid) == "payer_action_pending"
    assert p.mock.created_count() == 1
    assert ("create_invoice", "unknown") in _ops(p, iid)


# --- concurrency, leases, dead letter (A3) --------------------------------------------------
def test_concurrent_workers_claim_each_message_once(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    barrier = threading.Barrier(6)
    claimed: list = []

    def go(n):
        barrier.wait()
        claimed.extend(
            claim(
                p.engine,
                worker_id=f"w{n}",
                now=p.now,
                kinds=["create_payment_session"],
                lease_s=120,
            )
        )

    ts = [threading.Thread(target=go, args=(n,)) for n in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(claimed) == 1 and claimed[0].intent_id == intent["intent_id"]


def test_parallel_workers_create_exactly_one_invoice(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    errors = []

    def go(n):
        try:
            p.work(rounds=4, worker_id=f"w{n}")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert p.mock.created_count() == 1 and _calls(p, "send_invoice") == 1
    assert _status(p, intent["intent_id"]) == "payer_action_pending"


def test_lease_takeover_fences_the_old_worker(pilot):
    p = pilot
    p.bind()
    p.agree_and_approve()
    (a,) = claim(p.engine, worker_id="A", now=p.now, kinds=["create_payment_session"], lease_s=60)
    p.advance(seconds=61)
    (b,) = claim(p.engine, worker_id="B", now=p.now, kinds=["create_payment_session"], lease_s=60)
    assert b.id == a.id and b.lease_token == a.lease_token + 1
    with p.engine.begin() as c, pytest.raises(LeaseLostError):
        assert_lease(c, a, p.now)
    assert (
        finish(
            p.engine, a, __import__("api.workers.outbox").workers.outbox.DONE, p.now, RetryPolicy()
        )
        is False
    )
    with p.engine.begin() as c:
        assert_lease(c, b, p.now)


def test_retries_exhausted_dead_letter_keeps_cash_and_needs_review(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    with p.engine.begin() as c:
        c.execute(
            update(outbox_message).where(outbox_message.c.intent_id == iid).values(max_attempts=3)
        )
    p.mock.inject("send_invoice", *["before"] * 10)
    p.work(rounds=30, step_s=200)
    with p.engine.connect() as c:
        st = c.execute(
            select(outbox_message.c.status).where(
                outbox_message.c.intent_id == iid, outbox_message.c.kind == "create_payment_session"
            )
        ).scalar_one()
    assert st == "dead"
    assert _status(p, iid) == "exception" and "DEAD_LETTER" in _exceptions(p, iid)
    assert _released(p, iid)[0] is False  # money state uncertain: never auto-released


# --- reconciliation mismatches (A7) ---------------------------------------------------------
def _sent(p):
    p.bind()
    buyer, intent = p.agree_and_approve()
    p.work()
    iid = intent["intent_id"]
    assert _status(p, iid) == "payer_action_pending"
    return buyer, iid, p.invoice_id(iid)


def test_partial_payment_is_an_exception_without_posting(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.mock.pay(inv, amount_cents=1000)
    p.work()
    assert _status(p, iid) == "exception" and "PARTIAL_PAYMENT" in _exceptions(p, iid)
    assert _settlements(p) == 0 and _released(p, iid)[0] is False


def test_wrong_payee_on_capture_is_refused(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.mock.pay(inv, payee_merchant_id="SOMEONE-ELSE")
    p.work()
    assert "CAPTURE_MISMATCH" in _exceptions(p, iid) and _settlements(p) == 0


def test_marked_as_paid_is_not_provider_evidence(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.mock.mark_as_paid(inv)
    p.work()
    assert "MARKED_AS_PAID_NOT_PROVIDER_EVIDENCE" in _exceptions(p, iid)
    assert _settlements(p) == 0


def test_provider_side_total_change_is_refused(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.mock.pay(inv)
    p.mock.tamper(inv, amount={**p.mock.invoices[inv]["amount"], "value": "25.00"})
    p.work()
    assert "PAID_TERMS_MISMATCH" in _exceptions(p, iid) and _settlements(p) == 0


def test_pending_payment_then_completion(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    cap = p.mock.pay(inv, pending=True)
    p.work(rounds=3)
    assert _status(p, iid) == "provider_pending" and _settlements(p) == 0
    p.mock.complete_pending(cap)
    p.work()
    assert _status(p, iid) == "reconciled" and _settlements(p) == 1


def test_concurrent_reconcilers_book_exactly_once(pilot):
    from api.execution.service import _read, observe

    p = pilot
    _, iid, inv = _sent(p)
    p.mock.pay(inv)
    rts = [p.runtime(f"r{n}") for n in range(5)]
    # As in production, the invoice body comes from a recorded provider read: the booking
    # guard accepts only bodies Elide's own GETs recorded (ADR-0012 §17.1).
    body = _read(rts[0], iid, None, "get_invoice", p.mock.get_invoice, inv).body
    barrier = threading.Barrier(5)

    def go(rt):
        barrier.wait()
        observe(rt, iid, body, polling=False)

    ts = [threading.Thread(target=go, args=(rt,)) for rt in rts]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert _status(p, iid) == "reconciled" and _settlements(p) == 1
    with p.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM obligation_settlement_event")).scalar() == 1


def test_refund_after_reconciliation_is_reported_never_rewritten(pilot):
    from api.execution.service import schedule

    p = pilot
    _, iid, inv = _sent(p)
    cap = p.mock.pay(inv)
    p.work()
    assert _status(p, iid) == "reconciled"
    with p.engine.connect() as c:
        entries = c.execute(text("SELECT count(*) FROM journal_entry")).scalar()
    p.mock.refund(cap, 500)
    with p.engine.begin() as c:
        schedule(
            c,
            kind="provider_fetch",
            dedupe_key="fetch:test:refund",
            intent_id=None,
            payload={"capture_id": cap},
            at=p.now,
        )
    p.work()
    assert _status(p, iid) == "reconciled" and "REFUND_OBSERVED" in _exceptions(p, iid)
    with p.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM journal_entry")).scalar() == entries


def test_unknown_webhook_resource_is_recorded_and_ignored(pilot):
    from api.execution.service import schedule

    p = pilot
    _sent(p)
    with p.engine.begin() as c:
        schedule(
            c,
            kind="provider_fetch",
            dedupe_key="fetch:test:stranger",
            intent_id=None,
            payload={"invoice_id": "INV2-NOT-OURS-0001", "provider_event_id": "WH-X1"},
            at=p.now,
        )
    done = p.work()
    assert any(r.error == "UNMATCHED_RESOURCE" for _, r in done)
    assert _settlements(p) == 0


# --- expiry, cancellation, revocation (A2, E) ----------------------------------------------
def test_unpaid_by_deadline_is_cancelled_at_provider_then_released(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.advance(days=4)  # past the agreed pay-by date
    p.work(auto_cancel=True)  # the feature, explicitly enabled (off for the pilot)
    assert p.mock.invoices[inv]["status"] == "CANCELLED"
    assert _status(p, iid) == "expired" and _released(p, iid)[0] is True


def test_payer_cancel_before_dispatch_releases_immediately(pilot):
    p = pilot
    p.bind()
    p.app.state.payments_execution_enabled = False
    buyer, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    buyer.reauth()
    r = buyer.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/cancel", {}, key="cancel-key-1")
    assert r.status_code == 200, r.text and r.json()["cash_released"] is True
    assert _status(p, iid) == "cancelled" and active_commitment_total(p.engine, "pilot_buyer") == 0


def test_payer_cancel_after_send_waits_for_provider_cancellation(pilot):
    p = pilot
    buyer, iid, inv = _sent(p)
    buyer.reauth()
    r = buyer.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/cancel", {}, key="cancel-key-2")
    assert r.status_code == 200 and r.json()["cash_released"] is False
    assert _released(p, iid)[0] is False
    p.work()
    assert p.mock.invoices[inv]["status"] == "CANCELLED" and _status(p, iid) == "cancelled"
    assert _released(p, iid)[0] is True


def test_cancel_racing_a_payment_reconciles_the_payment(pilot):
    p = pilot
    buyer, iid, inv = _sent(p)
    buyer.reauth()
    buyer.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/cancel", {}, key="cancel-key-3")
    p.mock.pay(inv)  # the payer paid before the cancellation reached PayPal
    p.work()
    assert _status(p, iid) == "reconciled" and _settlements(p) == 1


def test_revoked_payer_authority_after_send_cancels_the_invoice(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    with p.engine.begin() as c:
        _revoke_buyer(c, p)
    p.work(auto_cancel=True)
    assert p.mock.invoices[inv]["status"] == "CANCELLED" and _status(p, iid) == "cancelled"


# --- isolation (A10) and database guards (A1) -----------------------------------------------
def test_payee_cannot_get_the_payer_link_and_outsiders_see_nothing(pilot):
    from api.app.auth import service as auth

    p = pilot
    _buyer, iid, _ = _sent(p)
    seller = p.actor(p.seller_login)
    seller.reauth()
    assert seller.get(f"/v1/m/pilot_seller/payment-intents/{iid}/payer-link").status_code == 404
    with p.engine.begin() as c:
        auth.create_merchant(
            c, "outsider", "Outsider (TEST)", "America/Toronto", is_synthetic=False
        )
        uid = auth.create_user(
            c, "outsider@elide.test", "Outsider", "test-only-outsider-pw-1", is_test_identity=True
        )
        auth.grant(c, uid, "outsider", "owner", at=p.now)
    from tests.integration.conftest import Actor

    out = Actor(p.app, "outsider@elide.test", "test-only-outsider-pw-1")
    for path in (
        f"/v1/m/outsider/payment-intents/{iid}",
        f"/v1/m/pilot_buyer/payment-intents/{iid}",
    ):
        assert out.get(path).status_code == 404
    assert out.get("/v1/m/outsider/settlements").json()["settlements"] == []
    out.reauth()
    assert (
        out.post(f"/v1/m/outsider/payment-intents/{iid}/cancel", {}, key="cancel-key-x").status_code
        == 404
    )


def test_database_refuses_illegal_transitions_and_term_changes(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    for values in (
        {"status": "reconciled"},
        {"amount_cents": 1},
        {"payee_merchant": "pilot_buyer"},
    ):
        with pytest.raises(DBAPIError), p.engine.begin() as c:
            c.execute(
                update(payment_intent).where(payment_intent.c.intent_id == iid).values(**values)
            )
    with pytest.raises(DBAPIError), p.engine.begin() as c:
        c.execute(text("DELETE FROM payment_intent WHERE intent_id = :i"), {"i": iid})


def test_double_release_and_unknown_never_release(pilot):
    p = pilot
    _, iid, _inv = _sent(p)
    p.mock.inject("get_invoice", *["before"] * 3)
    p.advance(seconds=61)
    p.work(rounds=2)
    assert _released(p, iid)[0] is False
    assert _calls(p, "cancel_invoice") == 0


__all__ = ["Job", "timedelta"]
