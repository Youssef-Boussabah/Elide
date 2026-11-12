"""P5 execution service: provider sessions, cancellation, reconciliation (ADR-0012).

Called by the outbox worker (`api/workers/outbox.py`) through the handlers at the bottom.
Rules that hold throughout:

- **Record before calling.** Every provider call has a `provider_operation` row committed
  before the call (mutating calls in the same transaction as the lease check and the
  revalidation) and finished after it. A crash in between leaves `started`, which is later
  treated as an unknown outcome.
- **No lock during I/O.** Database transactions are short; none spans a provider call.
- **Revalidate before every mutation** (§5): authority, conditions, independent verifier,
  commitment, bindings, kill switch.
- **Unknown is not failure.** A possibly-processed mutation moves the intent to
  `provider_unknown` and is resolved by lookup (search by invoice number / GET), never by a
  blind retry. A DRAFT invoice cannot be paid; only `send` makes it payable, and it is
  always preceded by a GET of that exact invoice.
- **Provider truth by GET.** State changes come from fetched resources, matched strictly
  against the expected terms; webhook bodies never change state.
- **Book atomically.** Settlement events, journal bundle, settlement record, commitment
  conversion and the intent state commit in one transaction, then the independent checker
  must agree.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from api.accounting.payer_postings import payer_entries
from api.accounting.reconciliation_check import verify_settlement
from api.accounting.settlement_postings import settlement_entries
from api.accounting.store import post_entries_in
from api.app.auth import service as auth_service
from api.app.auth.permissions import Permission
from api.app.auth.tables import merchant_org
from api.domain.invoice import Provenance, SettlementEvent, SettlementKind, SourceKind
from api.domain.money import USD, Money
from api.execution import bindings
from api.execution.invoice_session import (
    PAYABLE_OR_LATER,
    UNPAID,
    ExpectedInvoice,
    build_payload,
    invoice_number_for,
    terms_problems,
)
from api.execution.states import SESSION_LIVE, TERMINAL, IntentStatus, TransitionError, transition
from api.execution.tables import (
    obligation_settlement_event,
    payment_exception,
    payment_session,
    provider_operation,
    provider_request,
    provider_settlement,
    settlement_verification,
)
from api.integrations.paypal.execution import Outcome, PaymentProvider, ProviderResult
from api.integrations.paypal.invoice_terms import invoice_terms
from api.integrations.paypal.money import paypal_to_cents
from api.integrations.paypal.redaction import fingerprint
from api.integrations.paypal.settlement import settlement_from_bodies
from api.liquidity.store import release_commitment_in, settle_commitment_in
from api.workers.outbox import DONE, Job, Result, assert_lease
from api.workflow import audit, conditions
from api.workflow.networks import load_network
from api.workflow.plans import load_snapshot, verify_leg_now
from api.workflow.store import load_record
from api.workflow.tables import outbox_message, payment_authorization, payment_intent

S = IntentStatus
REFUNDISH = frozenset({"REFUNDED", "PARTIALLY_REFUNDED", "MARKED_AS_REFUNDED", "REFUNDED_EXTERNAL"})
EXTERNAL_PAID = frozenset({"MARKED_AS_PAID", "PAID_EXTERNAL"})
CANCELLED_AT_PROVIDER = frozenset({"CANCELLED", "AUTO_CANCELLED"})


@dataclass
class Runtime:
    engine: Engine
    provider: PaymentProvider
    payer_email: Callable[[], str | None]
    execution_enabled: Callable[[], bool]
    clock: Callable[[], datetime]
    worker_id: str = "worker"
    poll_s: float = 60.0
    lookup_delay_s: float = 30.0
    lookup_retries: int = 3
    stale_op_s: float = 300.0
    # Cancellation policy (gate review finding 1). False for the initial pilot: a passed
    # deadline or a revoked authority opens an operator review instead of cancelling.
    # Explicit payer cancellation (POST .../cancel) is unaffected.
    auto_cancel: bool = False
    review_poll_factor: float = 5.0

    @property
    def actor(self) -> str:
        return f"worker:{self.worker_id}"


class RevalidationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# -- small helpers ------------------------------------------------------------------------
def _sha(obj: Any) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _intent(conn: Connection, intent_id: str) -> dict[str, Any]:
    return dict(
        conn.execute(select(payment_intent).where(payment_intent.c.intent_id == intent_id))
        .mappings()
        .one()
    )


def _session(conn: Connection, intent_id: str) -> dict[str, Any] | None:
    r = (
        conn.execute(select(payment_session).where(payment_session.c.intent_id == intent_id))
        .mappings()
        .one_or_none()
    )
    return dict(r) if r else None


def _expected(sess: dict[str, Any], intent: dict[str, Any]) -> ExpectedInvoice:
    return ExpectedInvoice(
        session_id=sess["session_id"],
        invoice_number=sess["invoice_number"],
        obligation_id=intent["obligation_id"],
        revision_no=intent["revision_no"],
        item_cents=int(sess["expected_item_cents"]),
        discount_cents=int(sess["expected_discount_cents"]),
        total_cents=int(sess["expected_total_cents"]),
        due_date=sess["due_date"],
    )


def schedule(
    conn: Connection,
    *,
    kind: str,
    dedupe_key: str,
    intent_id: str | None,
    payload: dict[str, Any],
    at: datetime,
    revive: bool = False,
) -> None:
    """Enqueue work once per dedupe key. With `revive`, a finished message of the same key
    is put back to `pending` (e.g. a second cancellation request after a failed one)."""
    stmt = pg_insert(outbox_message).values(
        dedupe_key=dedupe_key,
        kind=kind,
        intent_id=intent_id,
        payload=payload,
        payload_sha256=_sha(payload),
        status="pending",
        available_at=at,
        created_at=at,
    )
    if revive:
        stmt = stmt.on_conflict_do_update(
            index_elements=["dedupe_key"],
            set_={"status": "pending", "available_at": at, "attempts": 0, "finished_at": None},
            where=outbox_message.c.status.in_(("done", "failed")),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    conn.execute(stmt)


def open_exception(
    conn: Connection, intent_id: str, code: str, detail: dict[str, Any], at: datetime
) -> None:
    conn.execute(
        pg_insert(payment_exception)
        .values(intent_id=intent_id, code=code[:60], detail=detail, opened_at=at)
        .on_conflict_do_nothing()
    )


def _to_exception(rt: Runtime, intent_id: str, code: str, detail: dict[str, Any]) -> Result:
    now = rt.clock()
    with rt.engine.begin() as conn:
        open_exception(conn, intent_id, code, detail, now)
        with contextlib.suppress(TransitionError):
            transition(
                conn,
                intent_id,
                S.EXCEPTION,
                expect={
                    S.PROVIDER_UNKNOWN,
                    S.PAYER_ACTION_PENDING,
                    S.PROVIDER_PENDING,
                    S.PROVIDER_CONFIRMED,
                },
                reason=code,
                actor=rt.actor,
                at=now,
                detail={"problems": detail.get("problems", [])[:10]},
            )
    return Result("done", error=code)


def _finish_without_payment(
    conn: Connection,
    intent: dict[str, Any],
    to: IntentStatus,
    *,
    reason: str,
    actor: str,
    at: datetime,
) -> None:
    """Terminal state with a confirmed absence of payment: release the reservation once."""
    transition(
        conn,
        intent["intent_id"],
        to,
        expect={S.APPROVED, S.SESSION_PENDING, S.PROVIDER_UNKNOWN, S.PAYER_ACTION_PENDING},
        reason=reason,
        actor=actor,
        at=at,
    )
    release_commitment_in(conn, intent["commitment_id"], at, f"{to.value}:{reason}"[:120])
    conn.execute(
        update(payment_session)
        .where(payment_session.c.intent_id == intent["intent_id"])
        .values(updated_at=at)
    )


# -- provider operations ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Attempt:
    """One provider call. Mutating calls belong to a logical request whose
    `PayPal-Request-Id` is stable across every attempt (gate review finding 2)."""

    op_id: int
    operation: str
    mutating: bool
    request_id: str | None
    attempt_no: int


def request_id_for(intent_id: str, operation: str) -> str:
    """Deterministic, unique per intent and call type (PayPal: unique per API call type)."""
    return f"elide-{intent_id}-{operation.removesuffix('_invoice')}"


def logical_request(
    conn: Connection, intent_id: str, operation: str, request_sha: str, at: datetime
) -> str:
    """The persisted request identity of (intent, operation). First use records the
    request digest; a later attempt with a different request is refused."""
    conn.execute(
        pg_insert(provider_request)
        .values(
            intent_id=intent_id,
            operation=operation,
            request_id=request_id_for(intent_id, operation),
            request_sha256=request_sha,
            created_at=at,
        )
        .on_conflict_do_nothing(constraint="uq_provider_request_logical")
    )
    row = conn.execute(
        select(provider_request.c.request_id, provider_request.c.request_sha256).where(
            provider_request.c.intent_id == intent_id, provider_request.c.operation == operation
        )
    ).one()
    if row.request_sha256 != request_sha:
        raise RevalidationError(
            "PROVIDER_REQUEST_CONFLICT",
            f"{operation} was first requested with different content; refusing to reuse "
            "its request identity for a changed request",
        )
    return row.request_id


def _begin_op(
    rt: Runtime,
    job: Job | None,
    intent_id: str,
    session_id: str | None,
    operation: str,
    *,
    mutating: bool,
    request: Any,
    check: Callable[[Connection], None] | None = None,
) -> Attempt:
    now = rt.clock()
    key = f"op-{secrets.token_hex(12)}"  # this attempt's row identity, never sent to PayPal
    with rt.engine.begin() as conn:
        if job is not None and mutating:
            assert_lease(conn, job, now)
        if check is not None:
            check(conn)  # raises RevalidationError: nothing is recorded or called
        request_id, attempt_no = None, 1
        if mutating:
            request_id = logical_request(conn, intent_id, operation, _sha(request), now)
            done = conn.execute(
                select(provider_operation.c.id).where(
                    provider_operation.c.request_id == request_id,
                    provider_operation.c.state == "succeeded",
                )
            ).first()
            if done is not None:
                # Never re-issue a mutation PayPal already confirmed (a duplicate dispatch
                # read stale state): re-read the provider instead.
                raise RevalidationError(
                    "REQUEST_ALREADY_SUCCEEDED", f"{operation} already succeeded; re-reading"
                )
            attempt_no = (
                1
                + conn.execute(
                    select(func.count())
                    .select_from(provider_operation)
                    .where(provider_operation.c.request_id == request_id)
                ).scalar_one()
            )
        try:
            with conn.begin_nested():
                op_id = conn.execute(
                    provider_operation.insert()
                    .values(
                        intent_id=intent_id,
                        session_id=session_id,
                        operation=operation,
                        mutating=mutating,
                        request_key=key,
                        request_sha256=_sha(request),
                        state="started",
                        worker=rt.worker_id,
                        started_at=now,
                        request_id=request_id,
                        attempt_no=attempt_no if mutating else None,
                    )
                    .returning(provider_operation.c.id)
                ).scalar_one()
        except IntegrityError as exc:
            raise RevalidationError(
                "PROVIDER_CALL_IN_FLIGHT", "another provider call is in flight"
            ) from exc
    return Attempt(op_id, operation, mutating, request_id, attempt_no)


def _conservative(att: Attempt, res: ProviderResult) -> ProviderResult:
    """A refusal of a *retried* mutation may mean the first attempt succeeded (a
    duplicate invoice number, or PayPal failing a duplicate request id), and a 409 on any
    mutation may be a concurrent duplicate. Neither proves that nothing happened: treat
    them as unknown and look the resource up."""
    if not att.mutating:
        return res
    refused = res.outcome in (Outcome.REJECTED, Outcome.NOT_FOUND)
    if (refused and att.attempt_no > 1) or res.http_status == 409:
        return replace(res, outcome=Outcome.UNKNOWN, error_name=f"RETRY_REFUSED:{res.error_name}")
    return res


def _finish_op(rt: Runtime, att: Attempt, res: ProviderResult) -> ProviderResult:
    res = _conservative(att, res)
    state = {Outcome.OK: "succeeded", Outcome.UNKNOWN: "unknown"}.get(res.outcome, "failed")
    outcome_code = (res.outcome.value + (f":{res.error_name}" if res.error_name else ""))[:60]
    now = rt.clock()
    with rt.engine.begin() as conn:
        n = conn.execute(
            update(provider_operation)
            .where(provider_operation.c.id == att.op_id, provider_operation.c.state == "started")
            .values(
                state=state,
                http_status=res.http_status,
                debug_id=(res.debug_id or None),
                outcome_code=outcome_code,
                response_sha256=_sha(res.body) if res.body is not None else None,
                finished_at=now,
            )
        ).rowcount
        if n == 0:
            # A late reply: another worker already reclassified this call after taking
            # over. The first record stands; the late fact is kept in the audit trail.
            audit.append(
                conn,
                at=now,
                action="provider.late_reply",
                object_type="provider_operation",
                object_id=str(att.op_id),
                detail={
                    "operation": att.operation,
                    "outcome": outcome_code,
                    "http_status": res.http_status,
                    "worker": rt.worker_id,
                },
            )
    return res


def _read(
    rt: Runtime, intent_id: str, session_id: str | None, op: str, fn, *args
) -> ProviderResult:
    att = _begin_op(rt, None, intent_id, session_id, op, mutating=False, request=list(args))
    return _finish_op(rt, att, fn(*args))


def _fenced(conn: Connection, job: Job | None, at: datetime) -> None:
    """Post-call writes of a mutating flow happen only while this worker still holds its
    lease, so a late reply to a worker that was taken over changes nothing."""
    if job is not None:
        assert_lease(conn, job, at)


def _resolve_stale_ops(rt: Runtime, intent_id: str) -> bool:
    """A mutating call left `started` longer than `stale_op_s` (its worker crashed) is an
    unknown outcome. Returns True if a fresh in-flight call exists (someone else is busy)."""
    now = rt.clock()
    busy = False
    with rt.engine.begin() as conn:
        rows = conn.execute(
            select(provider_operation.c.id, provider_operation.c.started_at).where(
                provider_operation.c.intent_id == intent_id,
                provider_operation.c.state == "started",
                provider_operation.c.mutating.is_(True),
            )
        ).all()
        for op_id, started in rows:
            if started < now - timedelta(seconds=rt.stale_op_s):
                conn.execute(
                    update(provider_operation)
                    .where(provider_operation.c.id == op_id)
                    .values(state="unknown", outcome_code="STALE_STARTED", finished_at=now)
                )
                with contextlib.suppress(TransitionError):
                    transition(
                        conn,
                        intent_id,
                        S.PROVIDER_UNKNOWN,
                        expect={S.SESSION_PENDING, S.PAYER_ACTION_PENDING, S.PROVIDER_PENDING},
                        reason="STALE_PROVIDER_CALL",
                        actor=rt.actor,
                        at=now,
                    )
            else:
                busy = True
    return busy


# -- revalidation (ADR-0012 §5) -----------------------------------------------------------
def revalidate(conn: Connection, rt: Runtime, intent: dict[str, Any], at: datetime) -> None:
    """Raise `RevalidationError` unless every condition of the approved payment still holds."""
    if not rt.execution_enabled():
        raise RevalidationError("EXECUTION_DISABLED", "payment execution is disabled")
    if intent["evidence_kind"] == "synthetic":
        raise RevalidationError("SYNTHETIC_EVIDENCE", "synthetic evidence never executes")
    a = (
        conn.execute(
            select(payment_authorization).where(
                payment_authorization.c.authorization_id == intent["authorization_id"]
            )
        )
        .mappings()
        .one()
    )
    if a["decision"] != "approve":
        raise RevalidationError("NOT_APPROVED", "the payment is not approved")
    if at >= a["expires_at"]:
        raise RevalidationError("AUTHORIZATION_EXPIRED", "the payment authorization expired")
    try:
        auth_service.lock_membership(
            conn, a["user_id"], intent["payer_merchant"], Permission.PAYMENT_AUTHORIZE
        )
    except (auth_service.NotFoundError, auth_service.ForbiddenError):
        raise RevalidationError(
            "AUTHORIZATION_REVOKED", "the approving user no longer holds payment authority"
        ) from None
    snap = load_snapshot(conn, intent["plan_id"], intent["payer_merchant"])
    record = snap["record"]
    leg = next(x for x in record["selected_legs"] if x["leg_id"] == a["option_id"])
    loaded = load_network(
        conn,
        snap["network_id"],
        initiator=record["focal_merchant"],
        as_of=at,
        executing_intent=intent["intent_id"],
    )
    cond = conditions.payment_conditions(conn, loaded, leg, at)
    if cond is None or conditions.digest(cond) != a["conditions_digest"]:
        raise RevalidationError(
            "CONDITIONS_CHANGED", "the conditions this payment was approved under have changed"
        )
    issues = verify_leg_now(loaded, leg, at)
    if issues:
        raise RevalidationError("VERIFIER_REJECTED", "; ".join(issues)[:300])
    active = conn.execute(
        select(func.count())
        .select_from(_commitments())
        .where(
            _commitments().c.commitment_id == intent["commitment_id"],
            _commitments().c.released_at.is_(None),
            _commitments().c.amount_cents
            == int(intent["amount_cents"]) + int(intent["fee_ceiling_cents"]),
        )
    ).scalar_one()
    if active != 1:
        raise RevalidationError("COMMITMENT_MISSING", "the cash reservation is not active")
    try:
        bindings.check_pair(
            conn,
            payer_merchant=intent["payer_merchant"],
            payee_merchant=intent["payee_merchant"],
            provider_credential_fingerprint=rt.provider.credential_fingerprint(),
            payer_email=rt.payer_email(),
        )
    except bindings.BindingError as exc:
        raise RevalidationError(exc.code, str(exc)) from None
    if rt.provider.environment != "sandbox":
        raise RevalidationError("NOT_SANDBOX", "only the PayPal Sandbox is permitted")


def _commitments():
    from api.liquidity.tables import capacity_commitment

    return capacity_commitment


def _new_session(conn: Connection, rt: Runtime, intent: dict[str, Any], at: datetime) -> None:
    rec = load_record(conn, intent["obligation_id"]).record
    rev = rec.in_force_revision(at)
    pos = rec.position(at)
    if rev.revision_no != intent["revision_no"]:
        raise RevalidationError("REVISION_CHANGED", "the invoice terms changed")
    amount = int(intent["amount_cents"])
    if pos.remaining_open.cents != amount:
        raise RevalidationError(
            "NOT_FULL_BALANCE", "the payment does not settle the remaining agreed balance"
        )
    if rev.due_at <= at:
        raise RevalidationError("DEADLINE_PASSED", "the agreed pay-by time has passed")
    tz = conn.execute(
        select(merchant_org.c.business_tz).where(
            merchant_org.c.merchant_id == intent["payee_merchant"]
        )
    ).scalar_one()
    inv = bindings.active(conn, intent["payee_merchant"], "invoicer")
    pay = bindings.active(conn, intent["payer_merchant"], "payer")
    if inv is None or pay is None:
        raise RuntimeError("invariant: bindings checked by revalidate")
    concession = rev.concession.cents
    conn.execute(
        payment_session.insert().values(
            session_id="ps_" + secrets.token_hex(8),
            intent_id=intent["intent_id"],
            provider="paypal",
            environment="sandbox",
            invoicer_account_id=inv.account_id,
            payer_account_id=pay.account_id,
            invoice_number=invoice_number_for(intent["intent_id"]),
            expected_item_cents=amount + concession,
            expected_discount_cents=concession,
            expected_total_cents=amount,
            currency=USD,
            due_date=rev.due_at.astimezone(ZoneInfo(tz)).date(),
            pay_deadline=rev.due_at,
            created_at=at,
            updated_at=at,
        )
    )


def _refused_before_call(rt: Runtime, intent_id: str, r: RevalidationError) -> Result:
    """No provider call was made. In flight elsewhere: back off. A conflicting request:
    operator review (cash stays reserved; the earlier request may have reached PayPal).
    Anything else: cancel before anything payable exists."""
    if r.code in ("PROVIDER_CALL_IN_FLIGHT", "REQUEST_ALREADY_SUCCEEDED"):
        return Result("retry", delay_s=rt.lookup_delay_s, error=r.code)
    if r.code == "PROVIDER_REQUEST_CONFLICT":
        _mark_unknown(rt, intent_id, r.code)
        return _to_exception(rt, intent_id, r.code, {"problems": [str(r)]})
    return _cancel_before_payable(rt, intent_id, r.code)


def _cancel_before_payable(rt: Runtime, intent_id: str, code: str) -> Result:
    """RevalidationError failed while no payable invoice exists (none, or an unsent DRAFT)."""
    now = rt.clock()
    with rt.engine.begin() as conn:
        intent = _intent(conn, intent_id)
        try:
            _finish_without_payment(conn, intent, S.CANCELLED, reason=code, actor=rt.actor, at=now)
        except TransitionError:
            return DONE
    return Result("done", error=code)


# -- handler: create_payment_session -------------------------------------------------------
def handle_create_session(rt: Runtime, job: Job) -> Result:
    intent_id = job.intent_id
    if intent_id is None:
        raise RuntimeError("invariant: intent_id is required here")
    with rt.engine.connect() as conn:
        intent = _intent(conn, intent_id)
    st = S(intent["status"])
    if st in TERMINAL or st in (S.PAYER_ACTION_PENDING, S.PROVIDER_PENDING, S.PROVIDER_CONFIRMED):
        return DONE
    if not rt.execution_enabled():
        return Result("retry", delay_s=rt.poll_s, error="EXECUTION_DISABLED")

    if st is S.APPROVED:
        now = rt.clock()
        try:
            with rt.engine.begin() as conn:
                assert_lease(conn, job, now)
                revalidate(conn, rt, intent, now)
                if _session(conn, intent_id) is None:
                    _new_session(conn, rt, intent, now)
                transition(
                    conn,
                    intent_id,
                    S.SESSION_PENDING,
                    expect={S.APPROVED},
                    reason="DISPATCHED",
                    actor=rt.actor,
                    at=now,
                )
        except TransitionError:
            # A duplicate dispatch message: another worker dispatched this intent first
            # (the session row is unique per intent). Re-read and continue from its state.
            return Result("retry", delay_s=rt.lookup_delay_s, error="ALREADY_DISPATCHED")
        except IntegrityError:
            return Result("retry", delay_s=rt.lookup_delay_s, error="ALREADY_DISPATCHED")
        except RevalidationError as r:
            return _cancel_before_payable(rt, intent_id, r.code)

    if _resolve_stale_ops(rt, intent_id):
        return Result("retry", delay_s=rt.lookup_delay_s, error="PROVIDER_CALL_IN_FLIGHT")
    with rt.engine.connect() as conn:
        intent = _intent(conn, intent_id)
        sess = _session(conn, intent_id)
    if sess is None:
        raise RuntimeError("invariant: sess is required here")
    e = _expected(sess, intent)
    email = rt.payer_email()
    if email is None:
        return Result("retry", delay_s=rt.poll_s, error="CREDENTIAL_NOT_CONFIGURED")

    def check(conn: Connection) -> None:
        cur = _intent(conn, intent_id)
        if S(cur["status"]) not in (S.SESSION_PENDING, S.PROVIDER_UNKNOWN):
            raise RevalidationError("STATE_CHANGED", f"intent is {cur['status']}")
        revalidate(conn, rt, cur, rt.clock())

    inv_id = sess["provider_invoice_id"]
    if inv_id is None:
        inv_id, early = _ensure_draft(rt, job, intent, sess, e, email, check)
        if early is not None:
            return early

    g = _read(rt, intent_id, sess["session_id"], "get_invoice", rt.provider.get_invoice, inv_id)
    if not g.ok:
        return Result("retry", error=f"GET_INVOICE:{g.outcome.value}")
    status = g.body.get("status")

    if status == "DRAFT":
        problems = terms_problems(
            g.body, e, invoice_id=inv_id, payer_email=email, expected_status="DRAFT"
        )
        if problems:
            # Not payable (a DRAFT): nothing can be paid, so the cash is released.
            now = rt.clock()
            with rt.engine.begin() as conn:
                open_exception(conn, intent_id, "DRAFT_TERMS_MISMATCH", {"problems": problems}, now)
            return _fail_unsent(rt, intent_id, "DRAFT_TERMS_MISMATCH", job)
        try:
            att = _begin_op(
                rt,
                job,
                intent_id,
                sess["session_id"],
                "send_invoice",
                mutating=True,
                request={"invoice_id": inv_id},
                check=check,
            )
        except RevalidationError as r:
            return _refused_before_call(rt, intent_id, r)
        res = _finish_op(rt, att, rt.provider.send_invoice(inv_id, att.request_id))
        if res.outcome is Outcome.RETRYABLE:
            return Result("retry", error="SEND:RETRYABLE")
        if res.outcome is Outcome.UNKNOWN:
            _mark_unknown(rt, intent_id, "SEND_OUTCOME_UNKNOWN", job)
        g = _read(rt, intent_id, sess["session_id"], "get_invoice", rt.provider.get_invoice, inv_id)
        if not g.ok:
            _mark_unknown(rt, intent_id, "SEND_NOT_CONFIRMED", job)
            return Result(
                "retry", delay_s=rt.lookup_delay_s, error=f"GET_AFTER_SEND:{g.outcome.value}"
            )
        status = g.body.get("status")
        if status == "DRAFT":
            if res.outcome in (Outcome.REJECTED, Outcome.FORBIDDEN, Outcome.NOT_FOUND):
                return _fail_unsent(
                    rt, intent_id, f"SEND_REJECTED:{res.error_name or res.http_status}", job
                )
            return Result("retry", delay_s=rt.lookup_delay_s, error="STILL_DRAFT")

    if status in PAYABLE_OR_LATER:
        problems = (
            terms_problems(
                g.body,
                e,
                invoice_id=inv_id,
                payer_email=email,
                expected_status=None,
                allowed_statuses=PAYABLE_OR_LATER,
            )
            if status in UNPAID
            else []
        )
        if problems:
            _mark_unknown(rt, intent_id, "SENT_TERMS_MISMATCH", job)
            return _to_exception(rt, intent_id, "SENT_TERMS_MISMATCH", {"problems": problems})
        now = rt.clock()
        with rt.engine.begin() as conn:
            _fenced(conn, job, now)
            transition(
                conn,
                intent_id,
                S.PAYER_ACTION_PENDING,
                expect={S.SESSION_PENDING, S.PROVIDER_UNKNOWN},
                reason="INVOICE_SENT",
                actor=rt.actor,
                at=now,
                detail={"provider_status": status},
            )
            conn.execute(
                update(payment_session)
                .where(payment_session.c.intent_id == intent_id)
                .values(sent_at=now, last_provider_status=status, updated_at=now)
            )
            schedule(
                conn,
                kind="reconcile_payment",
                dedupe_key=f"reconcile:{intent_id}",
                intent_id=intent_id,
                payload={"intent_id": intent_id},
                at=now if status not in UNPAID else now + timedelta(seconds=rt.poll_s),
            )
        return DONE
    _mark_unknown(rt, intent_id, f"UNEXPECTED_STATUS_{status}", job)
    return _to_exception(rt, intent_id, "UNEXPECTED_PROVIDER_STATUS", {"status": status})


def _mark_unknown(rt: Runtime, intent_id: str, reason: str, job: Job | None = None) -> None:
    with rt.engine.begin() as conn, contextlib.suppress(TransitionError):
        _fenced(conn, job, rt.clock())
        transition(
            conn,
            intent_id,
            S.PROVIDER_UNKNOWN,
            expect={S.SESSION_PENDING, S.PAYER_ACTION_PENDING, S.PROVIDER_PENDING},
            reason=reason,
            actor=rt.actor,
            at=rt.clock(),
        )


def _fail_unsent(rt: Runtime, intent_id: str, reason: str, job: Job | None = None) -> Result:
    now = rt.clock()
    with rt.engine.begin() as conn:
        _fenced(conn, job, now)
        intent = _intent(conn, intent_id)
        transition(
            conn,
            intent_id,
            S.FAILED,
            expect={S.SESSION_PENDING, S.PROVIDER_UNKNOWN},
            reason=reason[:80],
            actor=rt.actor,
            at=now,
        )
        release_commitment_in(conn, intent["commitment_id"], now, f"failed:{reason}"[:120])
    return Result("done", error=reason)


def _ensure_draft(rt, job, intent, sess, e, email, check):
    """Find or create the session's DRAFT invoice. Returns (invoice_id, None) or (None, Result)."""
    intent_id = intent["intent_id"]
    with rt.engine.connect() as conn:
        ops = conn.execute(
            select(provider_operation.c.operation, provider_operation.c.state)
            .where(provider_operation.c.intent_id == intent_id)
            .order_by(provider_operation.c.id)
        ).all()
    creates = [s for o, s in ops if o == "create_invoice"]
    maybe_created = any(s == "unknown" for s in creates)
    if maybe_created:
        last_create = max(i for i, (o, _) in enumerate(ops) if o == "create_invoice")
        searches = sum(
            1 for o, s in ops[last_create:] if o == "search_invoices" and s == "succeeded"
        )
        r = _read(
            rt,
            intent_id,
            sess["session_id"],
            "search_invoices",
            rt.provider.search_invoices,
            e.invoice_number,
        )
        if not r.ok:
            return None, Result("retry", delay_s=rt.lookup_delay_s, error="SEARCH_FAILED")
        found = [
            i
            for i in (r.body.get("items") or [])
            if (i.get("detail") or {}).get("invoice_number") == e.invoice_number
        ]
        if len(found) > 1:
            return None, _to_exception(
                rt,
                intent_id,
                "DUPLICATE_PROVIDER_INVOICES",
                {"count": len(found), "ids": [i.get("id") for i in found]},
            )
        if len(found) == 1:
            return _adopt(rt, intent_id, found[0]["id"], job), None
        if searches + 1 < rt.lookup_retries:
            return None, Result("retry", delay_s=rt.lookup_delay_s, error="LOOKUP_NOT_FOUND_YET")
        # Not found after repeated searches: re-create with the SAME invoice number and the
        # SAME PayPal-Request-Id. PayPal documents that a retried request id returns the
        # original result and that invoice numbers are unique; Elide relies on neither: a
        # late duplicate would be an unpayable DRAFT, detected by the next search, and a
        # refusal of this retry is treated as unknown (`_conservative`).
    payload = build_payload(e, email)
    try:
        # The digest covers the whole payload, recipient included (only its hash is stored).
        att = _begin_op(
            rt,
            job,
            intent_id,
            sess["session_id"],
            "create_invoice",
            mutating=True,
            request=payload,
            check=check,
        )
    except RevalidationError as r:
        return None, _refused_before_call(rt, intent_id, r)
    res = _finish_op(rt, att, rt.provider.create_invoice(payload, att.request_id))
    if res.ok and isinstance(res.body, dict) and res.body.get("id"):
        return _adopt(rt, intent_id, res.body["id"], job), None
    if res.outcome is Outcome.UNKNOWN or res.ok:
        _mark_unknown(rt, intent_id, "CREATE_OUTCOME_UNKNOWN", job)
        return None, Result("retry", delay_s=rt.lookup_delay_s, error="CREATE:UNKNOWN")
    if res.outcome is Outcome.RETRYABLE:
        return None, Result("retry", error="CREATE:RETRYABLE")
    # REJECTED / FORBIDDEN / NOT_FOUND: the provider refused before creating anything.
    return None, _fail_unsent(
        rt, intent_id, f"CREATE_REJECTED:{res.error_name or res.http_status}", job
    )


def _adopt(rt: Runtime, intent_id: str, invoice_id: str, job: Job | None = None) -> str:
    now = rt.clock()
    with rt.engine.begin() as conn:
        _fenced(conn, job, now)
        conn.execute(
            update(payment_session)
            .where(
                payment_session.c.intent_id == intent_id,
                payment_session.c.provider_invoice_id.is_(None),
            )
            .values(provider_invoice_id=invoice_id, draft_created_at=now, updated_at=now)
        )
        cur = S(_intent(conn, intent_id)["status"])
        if cur is S.PROVIDER_UNKNOWN:
            transition(
                conn,
                intent_id,
                S.SESSION_PENDING,
                expect={S.PROVIDER_UNKNOWN},
                reason="INVOICE_FOUND",
                actor=rt.actor,
                at=now,
            )
        sess = _session(conn, intent_id)
    if sess is None:
        raise RuntimeError("invariant: sess is required here")
    return sess["provider_invoice_id"]


# -- observation and booking (§7.2, §7.3) -------------------------------------------------
def observe(rt: Runtime, intent_id: str, body: dict[str, Any], *, polling: bool) -> Result:
    """Apply one fetched invoice to the intent. Only GET results reach this function."""
    now = rt.clock()
    with rt.engine.connect() as conn:
        intent = _intent(conn, intent_id)
        sess = _session(conn, intent_id)
    if sess is None:
        raise RuntimeError("invariant: sess is required here")
    st = S(intent["status"])
    status = body.get("status")
    terms = invoice_terms(body)
    txns = terms.payment_transaction_count
    with rt.engine.begin() as conn:
        conn.execute(
            update(payment_session)
            .where(payment_session.c.intent_id == intent_id)
            .values(last_provider_status=status, updated_at=now)
        )

    if st is S.RECONCILED:
        if status in REFUNDISH:
            with rt.engine.begin() as conn:
                open_exception(conn, intent_id, "REFUND_OBSERVED", {"provider_status": status}, now)
                audit.append(
                    conn,
                    at=now,
                    action="payment.refund_observed",
                    object_type="payment_intent",
                    object_id=intent_id,
                    merchant_id=intent["payee_merchant"],
                    detail={"provider_status": status},
                )
        return DONE
    if st in TERMINAL:
        return DONE
    rearm = Result("rearm", delay_s=rt.poll_s) if polling else DONE

    if status in UNPAID and txns == 0:
        if st is S.PROVIDER_PENDING:
            with rt.engine.begin() as conn:
                transition(
                    conn,
                    intent_id,
                    S.PAYER_ACTION_PENDING,
                    expect={S.PROVIDER_PENDING},
                    reason="PENDING_PAYMENT_DID_NOT_COMPLETE",
                    actor=rt.actor,
                    at=now,
                )
        if now >= sess["pay_deadline"]:
            if rt.auto_cancel:
                return request_cancel(rt, intent_id, target=S.EXPIRED, reason="PAY_DEADLINE_PASSED")
            return _needs_review(rt, intent, "PAY_DEADLINE_PASSED_REVIEW", status, polling)
        if not _authority_holds(rt, intent):
            if rt.auto_cancel:
                return request_cancel(
                    rt, intent_id, target=S.CANCELLED, reason="AUTHORIZATION_REVOKED"
                )
            return _needs_review(rt, intent, "AUTHORIZATION_REVOKED_REVIEW", status, polling)
        return rearm
    if status == "PAYMENT_PENDING":
        if st is not S.PROVIDER_PENDING:
            with rt.engine.begin() as conn:
                transition(
                    conn,
                    intent_id,
                    S.PROVIDER_PENDING,
                    expect={S.PAYER_ACTION_PENDING, S.PROVIDER_UNKNOWN},
                    reason="PROVIDER_PAYMENT_PENDING",
                    actor=rt.actor,
                    at=now,
                )
        return rearm
    if status == "PAID":
        return _confirm_and_book(rt, intent, sess, body, rearm)
    if status in EXTERNAL_PAID:
        return _to_exception(
            rt, intent_id, "MARKED_AS_PAID_NOT_PROVIDER_EVIDENCE", {"status": status}
        )
    if status == "PARTIALLY_PAID":
        return _to_exception(rt, intent_id, "PARTIAL_PAYMENT", {"paid_cents": terms.paid_cents})
    if status in REFUNDISH:
        return _to_exception(rt, intent_id, "REFUND_BEFORE_BOOKING", {"status": status})
    if status in CANCELLED_AT_PROVIDER and txns == 0:
        target = S.EXPIRED if now >= sess["pay_deadline"] else S.CANCELLED
        with rt.engine.begin() as conn:
            cur = _intent(conn, intent_id)
            if S(cur["status"]) in (S.PAYER_ACTION_PENDING, S.PROVIDER_UNKNOWN):
                _finish_without_payment(
                    conn, cur, target, reason=f"PROVIDER_{status}", actor=rt.actor, at=now
                )
                conn.execute(
                    update(payment_session)
                    .where(payment_session.c.intent_id == intent_id)
                    .values(cancelled_at=now)
                )
        return DONE
    return _to_exception(
        rt, intent_id, "UNEXPECTED_PROVIDER_STATUS", {"status": status, "transactions": txns}
    )


def _needs_review(
    rt: Runtime, intent: dict[str, Any], code: str, provider_status: str, polling: bool
) -> Result:
    """Automatic cancellation is disabled (the pilot policy): record that an operator must
    decide, change nothing financial (no cancel call, no release, no state change) and
    keep reconciling read-only, so a late payment is still booked exactly once."""
    now = rt.clock()
    with rt.engine.begin() as conn:
        inserted = conn.execute(
            pg_insert(payment_exception)
            .values(
                intent_id=intent["intent_id"],
                code=code,
                detail={"provider_status": provider_status, "auto_cancel": "disabled"},
                opened_at=now,
            )
            .on_conflict_do_nothing()
            .returning(payment_exception.c.id)
        ).first()
        if inserted is not None:
            audit.append(
                conn,
                at=now,
                action="payment.needs_review",
                object_type="payment_intent",
                object_id=intent["intent_id"],
                merchant_id=intent["payer_merchant"],
                detail={"code": code, "provider_status": provider_status},
            )
    if not polling:
        return Result("done", error=code)
    return Result("rearm", delay_s=rt.poll_s * rt.review_poll_factor, error=code)


def _authority_holds(rt: Runtime, intent: dict[str, Any]) -> bool:
    with rt.engine.begin() as conn:
        uid = conn.execute(
            select(payment_authorization.c.user_id).where(
                payment_authorization.c.authorization_id == intent["authorization_id"]
            )
        ).scalar_one()
        try:
            auth_service.lock_membership(
                conn, uid, intent["payer_merchant"], Permission.PAYMENT_AUTHORIZE
            )
            return True
        except (auth_service.NotFoundError, auth_service.ForbiddenError):
            return False


def _confirm_and_book(rt, intent, sess, inv_body, rearm) -> Result:
    intent_id = intent["intent_id"]
    email = rt.payer_email()
    e = _expected(sess, intent)
    inv_id = sess["provider_invoice_id"]
    problems = terms_problems(
        inv_body, e, invoice_id=inv_id, payer_email=email or "", expected_status="PAID"
    )
    if problems:
        return _to_exception(rt, intent_id, "PAID_TERMS_MISMATCH", {"problems": problems})
    cap_id = invoice_terms(inv_body).transaction_ids[0]
    c = _read(rt, intent_id, sess["session_id"], "get_capture", rt.provider.get_capture, cap_id)
    if not c.ok:
        return Result("rearm", delay_s=rt.lookup_delay_s, error=f"GET_CAPTURE:{c.outcome.value}")
    cap = c.body
    if cap.get("status") == "PENDING":
        return rearm
    with rt.engine.connect() as conn:
        inv_binding = bindings.by_id(conn, sess["invoicer_account_id"])
    cprob = []
    if cap.get("status") != "COMPLETED":
        cprob.append(f"capture status {cap.get('status')!r}")
    if cap.get("invoice_id") != inv_id:
        cprob.append("capture belongs to another invoice")
    try:
        gross = paypal_to_cents(
            (cap.get("seller_receivable_breakdown") or {}).get("gross_amount") or {}
        )
    except Exception:
        gross = None
    if gross != e.total_cents:
        cprob.append(f"capture gross {gross} != expected {e.total_cents}")
    payee_fp = fingerprint((cap.get("payee") or {}).get("merchant_id"))
    if payee_fp != inv_binding.account_fingerprint:
        cprob.append("capture payee is not the bound invoicer account (wrong payee)")
    if cprob:
        return _to_exception(rt, intent_id, "CAPTURE_MISMATCH", {"problems": cprob})
    try:
        return book(rt, intent, sess, inv_body, cap, payee_fp)
    except _BookingRefusedError as exc:
        return _to_exception(rt, intent_id, exc.code, {"problems": exc.problems})


class _BookingRefusedError(RuntimeError):
    def __init__(self, code: str, problems: list[str]):
        super().__init__(code)
        self.code = code
        self.problems = problems


def book(rt, intent, sess, inv_body, cap_body, payee_fp) -> Result:
    """One transaction: provider_confirmed -> reconciled with every financial record."""
    intent_id = intent["intent_id"]
    now = rt.clock()
    cap_id = cap_body["id"]
    try:
        settlement = settlement_from_bodies(
            inv_body,
            cap_body,
            f"provider_operation:get_invoice:{intent_id}",
            f"provider_operation:get_capture:{intent_id}",
        )
    except Exception as exc:
        raise _BookingRefusedError("SETTLEMENT_UNPARSEABLE", [str(exc)[:300]]) from None
    cash = Money(int(sess["expected_total_cents"]), USD)
    concession = Money(int(sess["expected_discount_cents"]), USD)
    paid_at = settlement.ordering_time()
    try:
        with rt.engine.begin() as conn:
            transition(
                conn,
                intent_id,
                S.PROVIDER_CONFIRMED,
                expect={S.PAYER_ACTION_PENDING, S.PROVIDER_PENDING, S.PROVIDER_UNKNOWN},
                reason="PROVIDER_PAID_VERIFIED",
                actor=rt.actor,
                at=now,
                detail={"capture_id": cap_id},
            )
            rec = load_record(conn, intent["obligation_id"], lock=True)
            entries = settlement_entries(settlement, intent["payee_merchant"]) + payer_entries(
                payer_entity=intent["payer_merchant"],
                obligation_id=intent["obligation_id"],
                capture_id=cap_id,
                cash=cash,
                concession=concession,
                recognized_at=rec.record.obligation.issued_at,
                paid_at=paid_at,
            )
            post_entries_in(conn, entries)
            event_id = f"paypal_capture:{cap_id.lower()}"
            conn.execute(
                obligation_settlement_event.insert().values(
                    event_id=event_id,
                    obligation_id=intent["obligation_id"],
                    kind=SettlementKind.CASH_APPLIED.value,
                    amount_cents=cash.cents,
                    currency=USD,
                    occurred_at=paid_at,
                    provenance_source=SourceKind.PAYPAL_SANDBOX.value,
                    provenance_reference=f"capture {cap_id}",
                    provenance_recorded_at=now,
                    provenance_evidence_ref=f"provider_operation:intent:{intent_id}",
                    intent_id=intent_id,
                )
            )
            # Re-apply every P1 rule (over-settlement, identity) from the stored rows.
            pos = load_record(conn, intent["obligation_id"]).record.position(max(now, paid_at))
            if pos.remaining_open.cents != 0:
                raise _BookingRefusedError("NOT_FULLY_SETTLED", [f"remaining {pos.remaining_open}"])
            bundle_sha = _sha([e.content_digest() for e in entries])
            rate = settlement.exchange_rate
            conn.execute(
                provider_settlement.insert().values(
                    capture_id=cap_id,
                    provider="paypal",
                    environment="sandbox",
                    invoice_id=settlement.invoice_id,
                    intent_id=intent_id,
                    payer_merchant=intent["payer_merchant"],
                    payee_merchant=intent["payee_merchant"],
                    receipt_id=f"paypal_capture:{cap_id.lower()}",
                    gross_cents=settlement.gross.cents,
                    gross_currency=settlement.gross.currency,
                    fee_cents=settlement.provider_fee.cents,
                    net_cents=settlement.net.cents,
                    credit_cents=settlement.settlement_credit.cents,
                    credit_currency=settlement.settlement_credit.currency,
                    exchange_rate=str(rate.rate) if rate else None,
                    capture_status=settlement.capture_status,
                    invoice_status=settlement.invoice_status,
                    capture_create_time=paid_at,
                    payee_fingerprint=payee_fp,
                    bundle_sha256=bundle_sha,
                    check_sha256=None,  # legacy column (0008)
                    recorded_at=now,
                )
            )
            settle_commitment_in(
                conn,
                reservation_id=intent["commitment_id"],
                settled_id=f"so:{intent_id}",
                outflow_cents=cash.cents,
                at=now,
                reason=f"settled:capture:{cap_id}",
            )
            # The independent checker (ADR-0012 §17.1): the raw rows written above against
            # PayPal's own bodies. Any disagreement refuses the whole booking.
            ver = verify_settlement(
                conn, capture_id=cap_id, invoice_body=inv_body, capture_body=cap_body
            )
            problems = ver.problems
            if problems:
                raise _BookingRefusedError("INDEPENDENT_CHECK_FAILED", problems)
            conn.execute(
                settlement_verification.insert().values(
                    capture_id=cap_id,
                    intent_id=intent_id,
                    checker_version=ver.checker_version,
                    purpose="booking",
                    verdict="passed",
                    problems=[],
                    document=ver.document,
                    document_sha256=ver.digest,
                    invoice_body_sha256=ver.document["inputs"]["invoice_body_sha256"],
                    capture_body_sha256=ver.document["inputs"]["capture_body_sha256"],
                    verified_at=now,
                    actor=rt.actor,
                )
            )
            transition(
                conn,
                intent_id,
                S.RECONCILED,
                expect={S.PROVIDER_CONFIRMED},
                reason="BOOKED_AND_CHECKED",
                actor=rt.actor,
                at=now,
                detail={
                    "capture_id": cap_id,
                    "bundle_sha256": bundle_sha,
                    "verification_sha256": ver.digest,
                    "checker_version": ver.checker_version,
                },
            )
            conn.execute(
                update(payment_session)
                .where(payment_session.c.intent_id == intent_id)
                .values(last_provider_status="PAID", updated_at=now)
            )
    except TransitionError:
        return DONE  # another worker booked it first (one posting only)
    except IntegrityError as exc:
        raise _BookingRefusedError(
            "DUPLICATE_OR_CONFLICTING_POSTING", [str(exc.orig)[:300]]
        ) from None
    return DONE


# -- cancellation --------------------------------------------------------------------------
def request_cancel(rt: Runtime, intent_id: str, *, target: IntentStatus, reason: str) -> Result:
    now = rt.clock()
    with rt.engine.begin() as conn:
        conn.execute(
            update(payment_session)
            .where(
                payment_session.c.intent_id == intent_id,
                payment_session.c.cancel_requested_at.is_(None),
            )
            .values(cancel_requested_at=now, updated_at=now)
        )
        schedule(
            conn,
            kind="cancel_payment_session",
            dedupe_key=f"cancel:{intent_id}",
            intent_id=intent_id,
            payload={"intent_id": intent_id, "target": target.value, "reason": reason},
            at=now,
            revive=True,
        )
    return Result("done", error=reason)


def handle_cancel(rt: Runtime, job: Job) -> Result:
    intent_id = job.intent_id
    if intent_id is None:
        raise RuntimeError("invariant: intent_id is required here")
    target = S(job.payload.get("target", "cancelled"))
    reason = str(job.payload.get("reason", "CANCEL_REQUESTED"))[:60]
    now = rt.clock()
    with rt.engine.connect() as conn:
        intent = _intent(conn, intent_id)
        sess = _session(conn, intent_id)
    st = S(intent["status"])
    if st in TERMINAL:
        return DONE
    if sess is None or sess["provider_invoice_id"] is None:
        if st in (S.APPROVED, S.SESSION_PENDING):
            with rt.engine.begin() as conn:
                _finish_without_payment(
                    conn,
                    _intent(conn, intent_id),
                    S.CANCELLED,
                    reason=reason,
                    actor=rt.actor,
                    at=now,
                )
            return DONE
        return Result("retry", delay_s=rt.lookup_delay_s, error="CANCEL_WAITS_FOR_LOOKUP")
    inv_id = sess["provider_invoice_id"]
    g = _read(rt, intent_id, sess["session_id"], "get_invoice", rt.provider.get_invoice, inv_id)
    if not g.ok:
        return Result("retry", error=f"GET_INVOICE:{g.outcome.value}")
    status = g.body.get("status")
    if status == "DRAFT":
        with rt.engine.begin() as conn:
            cur = _intent(conn, intent_id)
            final = S.CANCELLED if S(cur["status"]) is S.SESSION_PENDING else target
            _finish_without_payment(conn, cur, final, reason=reason, actor=rt.actor, at=now)
        return DONE
    if status in CANCELLED_AT_PROVIDER or status in UNPAID:
        if status in UNPAID:
            if not rt.execution_enabled():
                return Result("retry", delay_s=rt.poll_s, error="EXECUTION_DISABLED")
            try:
                att = _begin_op(
                    rt,
                    job,
                    intent_id,
                    sess["session_id"],
                    "cancel_invoice",
                    mutating=True,
                    request={"invoice_id": inv_id},
                )
            except RevalidationError as r:
                if r.code == "PROVIDER_REQUEST_CONFLICT":
                    return _to_exception(rt, intent_id, r.code, {"problems": [str(r)]})
                return Result("retry", delay_s=rt.lookup_delay_s, error=r.code)  # incl. done
            res = _finish_op(rt, att, rt.provider.cancel_invoice(inv_id, att.request_id))
            if res.outcome in (Outcome.UNKNOWN, Outcome.RETRYABLE):
                return Result(
                    "retry", delay_s=rt.lookup_delay_s, error=f"CANCEL:{res.outcome.value}"
                )
            g = _read(
                rt, intent_id, sess["session_id"], "get_invoice", rt.provider.get_invoice, inv_id
            )
            if not g.ok:
                return Result("retry", error="GET_AFTER_CANCEL")
            status = g.body.get("status")
        if status in CANCELLED_AT_PROVIDER and invoice_terms(g.body).payment_transaction_count == 0:
            with rt.engine.begin() as conn:
                cur = _intent(conn, intent_id)
                _finish_without_payment(conn, cur, target, reason=reason, actor=rt.actor, at=now)
                conn.execute(
                    update(payment_session)
                    .where(payment_session.c.intent_id == intent_id)
                    .values(cancelled_at=now, last_provider_status=status, updated_at=now)
                )
            return DONE
    # Paid, pending or otherwise moved on before the cancellation: reconcile the fact.
    return observe(rt, intent_id, g.body, polling=False)


# -- reconcile / webhook fetch ------------------------------------------------------------
def handle_reconcile(rt: Runtime, job: Job) -> Result:
    intent_id = job.intent_id
    if intent_id is None:
        raise RuntimeError("invariant: intent_id is required here")
    with rt.engine.connect() as conn:
        intent = _intent(conn, intent_id)
        sess = _session(conn, intent_id)
    if S(intent["status"]) in TERMINAL - {S.RECONCILED} or sess is None:
        return DONE
    if sess["provider_invoice_id"] is None:
        return DONE
    if S(intent["status"]) is S.RECONCILED:
        return DONE
    g = _read(
        rt,
        intent_id,
        sess["session_id"],
        "get_invoice",
        rt.provider.get_invoice,
        sess["provider_invoice_id"],
    )
    if not g.ok:
        return Result("retry", error=f"GET_INVOICE:{g.outcome.value}")
    return observe(rt, intent_id, g.body, polling=True)


def handle_provider_fetch(rt: Runtime, job: Job) -> Result:
    """A verified webhook named a resource. Find the session it belongs to and fetch it."""
    p = job.payload
    inv_id = p.get("invoice_id")
    if inv_id is None and p.get("capture_id"):
        with rt.engine.connect() as conn:
            known = conn.execute(
                select(provider_settlement.c.invoice_id).where(
                    provider_settlement.c.capture_id == p["capture_id"]
                )
            ).scalar_one_or_none()
        if known is None:
            c = rt.provider.get_capture(p["capture_id"])
            if c.outcome is Outcome.RETRYABLE:
                return Result("retry", error="GET_CAPTURE")
            known = c.body.get("invoice_id") if c.ok and isinstance(c.body, dict) else None
        inv_id = known
    with rt.engine.connect() as conn:
        sess = (
            None
            if inv_id is None
            else conn.execute(
                select(payment_session).where(payment_session.c.provider_invoice_id == inv_id)
            )
            .mappings()
            .one_or_none()
        )
    if sess is None:
        with rt.engine.begin() as conn:
            audit.append(
                conn,
                at=rt.clock(),
                action="webhook.unmatched",
                object_type="webhook",
                object_id=str(p.get("provider_event_id", "?"))[:120],
                detail={"event_type": p.get("event_type")},
            )
        return Result("done", error="UNMATCHED_RESOURCE")
    g = _read(
        rt, sess["intent_id"], sess["session_id"], "get_invoice", rt.provider.get_invoice, inv_id
    )
    if not g.ok:
        return Result("retry", error=f"GET_INVOICE:{g.outcome.value}")
    return observe(rt, sess["intent_id"], g.body, polling=False)


def on_dead(rt: Runtime, job: Job, error: str) -> None:
    """Retries exhausted: the money state may be uncertain, so an operator decides."""
    if job.intent_id is None:
        return
    with rt.engine.begin() as conn:
        open_exception(
            conn, job.intent_id, "DEAD_LETTER", {"kind": job.kind, "error": error[:200]}, rt.clock()
        )
        st = S(_intent(conn, job.intent_id)["status"])
        if st in SESSION_LIVE or st is S.PROVIDER_CONFIRMED:
            if st is S.SESSION_PENDING:
                transition(
                    conn,
                    job.intent_id,
                    S.PROVIDER_UNKNOWN,
                    expect={S.SESSION_PENDING},
                    reason="DEAD_LETTER",
                    actor=rt.actor,
                    at=rt.clock(),
                )
            transition(
                conn,
                job.intent_id,
                S.EXCEPTION,
                expect={
                    S.PROVIDER_UNKNOWN,
                    S.PAYER_ACTION_PENDING,
                    S.PROVIDER_PENDING,
                    S.PROVIDER_CONFIRMED,
                },
                reason="DEAD_LETTER",
                actor=rt.actor,
                at=rt.clock(),
            )


HANDLERS: dict[str, Callable[[Runtime, Job], Result]] = {
    "create_payment_session": handle_create_session,
    "reconcile_payment": handle_reconcile,
    "provider_fetch": handle_provider_fetch,
    "cancel_payment_session": handle_cancel,
}
MUTATING_KINDS = frozenset({"create_payment_session", "cancel_payment_session"})


def handlers_for(rt: Runtime) -> dict[str, Callable[[Job], Result]]:
    return {k: (lambda job, f=f: f(rt, job)) for k, f in HANDLERS.items()}


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "HANDLERS",
    "MUTATING_KINDS",
    "Provenance",
    "RevalidationError",
    "Runtime",
    "SettlementEvent",
    "book",
    "handlers_for",
    "observe",
    "on_dead",
    "request_cancel",
    "revalidate",
    "schedule",
]
