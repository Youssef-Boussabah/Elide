"""P5 payment-gate review regressions (owner review of 613d72f, 2026-11-12).

1. Cancellation policy: automatic cancellation is excluded from the initial pilot. Passing
   the pay deadline must not produce `POST .../cancel`, must not release cash and must not
   call anything provider-cancelled unless PayPal says so; read-only reconciliation goes on.
2. Request identity: each logical create/send/cancel has one stable, persisted
   `PayPal-Request-Id`, reused on every retry; conflicting payloads are refused.

The first two tests are the reproductions: both failed on 613d72f
(`evidence/phase5/gate_review_reproduction_2026-11-12.md`).
**Synthetic-tested** (mock provider); no PayPal call.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from api.execution.service import RevalidationError, logical_request, request_id_for, schedule
from api.execution.tables import payment_exception, provider_request, provider_settlement
from api.liquidity.tables import capacity_commitment
from api.workflow.tables import audit_event

pytestmark = pytest.mark.integration


def _sent(p):
    p.bind()
    buyer, intent = p.agree_and_approve()
    p.work()
    iid = intent["intent_id"]
    assert p.intent(iid)["status"] == "payer_action_pending"
    return buyer, iid, p.invoice_id(iid)


def _cancel_calls(p):
    return [c for c in p.mock.calls if c[0] == "cancel_invoice"]


def _open_codes(p, iid):
    with p.engine.connect() as c:
        return {
            code
            for (code,) in c.execute(
                select(payment_exception.c.code).where(
                    payment_exception.c.intent_id == iid, payment_exception.c.resolved_at.is_(None)
                )
            )
        }


def _reserved(p, iid):
    with p.engine.connect() as c:
        released = c.execute(
            select(capacity_commitment.c.released_at).where(
                capacity_commitment.c.commitment_id == f"pi:{iid}"
            )
        ).scalar_one()
    return released is None


def _ids(p, op):
    return [k for o, k in p.mock.request_log if o == op]


def _request_rows(p, iid):
    with p.engine.connect() as c:
        return c.execute(
            select(provider_request.c.operation, provider_request.c.request_id).where(
                provider_request.c.intent_id == iid
            )
        ).all()


def _revoke_buyer(p):
    from api.app.auth import service as auth

    with p.engine.begin() as c:
        auth.revoke_membership(
            c,
            p.seeded.users[p.buyer_login],
            "pilot_buyer",
            at=p.now,
            revoked_by=None,
            reason="test: approver authority withdrawn",
        )


# --- reproduction of finding 1 --------------------------------------------------------------
def test_deadline_passing_never_cancels_when_auto_cancel_is_disabled(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.advance(days=4)  # past the agreed pay-by date
    p.work()
    assert _cancel_calls(p) == []
    assert p.mock.invoices[inv]["status"] == "SENT"
    assert p.intent(iid)["status"] == "payer_action_pending"


# --- reproduction of finding 2 --------------------------------------------------------------
def test_a_retried_create_reuses_its_original_request_id(pilot):
    p = pilot
    p.bind()
    p.agree_and_approve()
    p.mock.inject("create_invoice", "before")  # attempt 1 never reaches PayPal
    p.work()
    creates = _ids(p, "create_invoice")
    assert len(creates) == 2
    assert creates[0] == creates[1]


# --- finding 1: cancellation policy (disabled for the pilot) --------------------------------
def test_passed_deadline_opens_review_keeps_cash_and_keeps_reconciling(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    for _ in range(3):  # repeated polls never escalate to a cancellation
        p.advance(days=2)
        p.work()
    assert _cancel_calls(p) == [] and p.mock.invoices[inv]["status"] == "SENT"
    assert _reserved(p, iid) and "PAY_DEADLINE_PASSED_REVIEW" in _open_codes(p, iid)
    assert p.intent(iid)["status"] == "payer_action_pending"  # never "cancelled" locally
    gets = sum(1 for op, _ in p.mock.calls if op == "get_invoice")
    p.advance(hours=2)
    p.work()
    assert sum(1 for op, _ in p.mock.calls if op == "get_invoice") > gets  # still polling


def test_late_payment_after_the_deadline_is_still_booked_once(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.advance(days=4)
    p.work()
    p.mock.pay(inv)
    p.advance(hours=1)
    p.work()
    assert p.intent(iid)["status"] == "reconciled" and _cancel_calls(p) == []
    with p.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(provider_settlement)).scalar() == 1


def test_only_a_provider_confirmed_cancellation_ends_the_payment(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    p.advance(days=4)
    p.work()
    assert _reserved(p, iid)
    p.mock.invoices[inv]["status"] = "CANCELLED"  # cancelled at PayPal, outside Elide
    p.advance(hours=1)
    p.work()
    assert p.intent(iid)["status"] == "expired" and not _reserved(p, iid)
    assert _cancel_calls(p) == []  # Elide itself never cancelled


def test_revoked_authority_after_send_opens_review_instead_of_cancelling(pilot):
    p = pilot
    _, iid, inv = _sent(p)
    _revoke_buyer(p)
    p.advance(minutes=2)
    p.work()
    assert _cancel_calls(p) == [] and p.mock.invoices[inv]["status"] == "SENT"
    assert "AUTHORIZATION_REVOKED_REVIEW" in _open_codes(p, iid) and _reserved(p, iid)


def test_explicit_payer_cancellation_still_works_with_auto_cancel_disabled(pilot):
    p = pilot
    buyer, iid, inv = _sent(p)
    buyer.reauth()
    r = buyer.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/cancel", {}, key="cancel-gate-1")
    assert r.status_code == 200, r.text
    p.work()
    assert len(_cancel_calls(p)) == 1 and p.mock.invoices[inv]["status"] == "CANCELLED"
    assert p.intent(iid)["status"] == "cancelled" and not _reserved(p, iid)


def test_payer_link_is_refused_after_the_deadline(pilot):
    p = pilot
    _, iid, _ = _sent(p)
    p.advance(days=4)
    buyer = p.actor(p.buyer_login)  # a fresh sign-in (sessions end after 8 hours)
    r = buyer.get(f"/v1/m/pilot_buyer/payment-intents/{iid}/payer-link")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PAY_DEADLINE_PASSED"


def test_kill_switch_still_reconciles_and_never_cancels_after_the_deadline(pilot):
    p = pilot
    _, iid, _ = _sent(p)
    p.flags["execution"] = False
    p.advance(days=4)
    p.work(auto_cancel=True)  # even with the policy on, the kill switch forbids POST cancel
    assert _cancel_calls(p) == [] and _reserved(p, iid)


# --- finding 2: stable, persisted request identity ------------------------------------------
@pytest.mark.parametrize("honor", [True, False])
def test_timeout_after_creation_with_slow_search_never_duplicates(pilot, honor):
    """The create is processed, the reply lost, and search does not see the invoice for
    longer than the lookup budget, so Elide re-creates with the same number AND the same
    request id. Whether PayPal honours the id (returns the original) or refuses the
    duplicate number, the result is one invoice and no false failure."""
    p = pilot
    p.mock.honor_request_id = honor
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    p.mock.inject("create_invoice", "after")
    p.mock.search_lag(4)
    p.work(rounds=40)
    assert p.mock.created_count() == 1
    assert p.intent(iid)["status"] == "payer_action_pending"
    ids = _ids(p, "create_invoice")
    assert len(ids) >= 2 and len(set(ids)) == 1
    assert {op for op, _ in _request_rows(p, iid)} == {"create_invoice", "send_invoice"}


def test_send_and_cancel_retries_reuse_their_request_ids(pilot):
    p = pilot
    p.bind()
    buyer, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    p.mock.inject("send_invoice", "before")
    p.work()
    sends = _ids(p, "send_invoice")
    assert len(sends) == 2 and len(set(sends)) == 1
    p.mock.inject("cancel_invoice", "before")  # attempt 1 never reaches PayPal
    buyer.reauth()
    r = buyer.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/cancel", {}, key="cancel-gate-2")
    assert r.status_code == 200, r.text
    p.work()
    cancels = _ids(p, "cancel_invoice")
    assert len(cancels) == 2 and len(set(cancels)) == 1
    assert p.intent(iid)["status"] == "cancelled"
    assert len({r for _, r in _request_rows(p, iid)}) == 3  # create, send, cancel


def test_crash_and_takeover_reuses_the_request_id(pilot, monkeypatch):
    import api.execution.service as svc

    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    real = svc._finish_op
    crashed = {"done": False}

    def crash_once(rt, att, res):
        if att.operation == "create_invoice" and not crashed["done"]:
            crashed["done"] = True
            raise SystemExit("worker killed after PayPal created the draft")
        return real(rt, att, res)

    monkeypatch.setattr(svc, "_finish_op", crash_once)
    with pytest.raises(SystemExit):
        p.work(rounds=1)
    monkeypatch.setattr(svc, "_finish_op", real)
    p.mock.search_lag(5)  # the takeover cannot find it by search: it must re-create
    p.advance(minutes=10)
    p.work(worker_id="w2", rounds=40)
    assert p.mock.created_count() == 1 and p.intent(iid)["status"] == "payer_action_pending"
    assert len(set(_ids(p, "create_invoice"))) == 1


def test_duplicate_dispatch_creates_one_invoice_with_one_identity(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    with p.engine.begin() as c:  # a second dispatch message for the same intent
        schedule(
            c,
            kind="create_payment_session",
            dedupe_key=f"pay-dup:{iid}",
            intent_id=iid,
            payload={"intent_id": iid},
            at=p.now,
        )
    # Three workers race on the two messages; the shared test clock moves in small steps
    # so leases do not expire under each other. A final pass drains any retries.
    ts = [
        threading.Thread(target=p.work, kwargs={"rounds": 6, "worker_id": f"d{n}", "step_s": 1.0})
        for n in range(3)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    p.work(worker_id="drain")
    assert p.intent(iid)["status"] == "payer_action_pending"
    assert p.mock.created_count() == 1
    assert sum(1 for o, _ in p.mock.calls if o == "send_invoice") == 1
    assert len(set(_ids(p, "create_invoice"))) == 1


def test_conflicting_payload_never_reuses_an_identity(pilot):
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    # An earlier, different create request already holds this intent's identity.
    with p.engine.begin() as c:
        c.execute(
            provider_request.insert().values(
                intent_id=iid,
                operation="create_invoice",
                request_id=request_id_for(iid, "create_invoice"),
                request_sha256="0" * 64,
                created_at=p.now,
            )
        )
    p.work()
    assert p.mock.created_count() == 0 and _ids(p, "create_invoice") == []
    assert p.intent(iid)["status"] == "exception" and _reserved(p, iid)
    assert "PROVIDER_REQUEST_CONFLICT" in _open_codes(p, iid)
    with p.engine.begin() as c, pytest.raises(RevalidationError) as exc:
        logical_request(c, iid, "create_invoice", "1" * 64, p.now)
    assert exc.value.code == "PROVIDER_REQUEST_CONFLICT"
    with p.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(provider_request)).scalar() == 1


def test_late_reply_from_a_taken_over_worker_changes_nothing(pilot, monkeypatch):
    """Worker A's create is slow. Its lease expires, worker B takes over, finds the invoice
    and sends it. Then A's reply arrives: recorded as a late reply, and A stops."""
    p = pilot
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    real_create = p.mock.create_invoice
    state = {"inside": False}

    def slow_create(payload, request_key):
        res = real_create(payload, request_key)
        if not state["inside"]:
            state["inside"] = True
            p.advance(minutes=10)  # A's lease and its in-flight call go stale
            p.work(worker_id="B")  # runs while A is still "on the wire": no DB lock held
        return res

    monkeypatch.setattr(p.mock, "create_invoice", slow_create)
    p.work(worker_id="A", rounds=1)
    assert p.mock.created_count() == 1
    assert sum(1 for o, _ in p.mock.calls if o == "send_invoice") == 1
    assert p.intent(iid)["status"] == "payer_action_pending"
    with p.engine.connect() as c:
        late = (
            c.execute(
                select(audit_event.c.detail).where(audit_event.c.action == "provider.late_reply")
            )
            .scalars()
            .all()
        )
    assert len(late) == 1 and late[0]["worker"] == "A"
