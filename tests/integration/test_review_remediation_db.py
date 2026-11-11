"""P1 review remediation against real PostgreSQL 16 (throwaway databases, real migrations).

Finding 2: evidence round trip, replay, restart, duplicate receipts, supersession.
Finding 5: reversal evidence classes persisted and checked against the original.
Finding 6: the settlement accounting bundle is imported atomically and its
reconciliation status is derived from what is actually booked.
"""

import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from api.accounting import store
from api.accounting.journal import (
    EntryKind,
    EvidenceClass,
    JournalEntry,
    Posting,
    ProviderEventRef,
)
from api.accounting.settlement_import import (
    BundleStatus,
    import_settlement,
    settlement_import_status,
)
from api.accounting.settlement_postings import settlement_entries
from api.accounting.tables import journal_entry
from api.domain.invoice import Provenance, SourceKind
from api.domain.money import USD, Money, SignedAmount
from api.integrations.paypal.settlement import load_settlement
from api.liquidity import store as cap_store
from api.liquidity.capacity import (
    AvailabilityBasis,
    AvailabilityEvidence,
    CapacityEvidence,
    Commitment,
    EvidenceConflictError,
    EvidenceKind,
    IncomingReceipt,
    InsufficientCapacityError,
    ReceiptConflictError,
    ReceiptStatus,
    SupersededEvidenceError,
)

pytestmark = pytest.mark.integration

RUNS = Path(__file__).resolve().parents[2] / "evidence" / "paypal-sandbox" / "runs"
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
ATTESTED = Provenance(SourceKind.MERCHANT_ATTESTED, "integration-test attestation", T0)
ENTITY = "paypal_sandbox_merchant"


def settlement():
    return load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")


def evidence(eid, merchant, spendable=0, reserve=0, observed=T0, included=frozenset(), **kw):
    return CapacityEvidence(
        eid,
        merchant,
        Money(spendable, USD),
        Money(reserve, USD),
        EvidenceKind.MERCHANT_ATTESTED,
        observed,
        kw.pop("valid_until", observed + timedelta(hours=8)),
        ATTESTED,
        included_receipt_ids=included,
    )


def receipt(rid, merchant, cents=99000, at=T0):
    return IncomingReceipt(
        rid,
        merchant,
        Money(cents, USD),
        ReceiptStatus.AVAILABLE_VERIFIED,
        ATTESTED,
        at,
        AvailabilityEvidence(AvailabilityBasis.MERCHANT_ATTESTED, f"att:{rid}", at, merchant),
    )


# --- finding 2: evidence persistence --------------------------------------------------


def test_evidence_round_trip_preserves_included_receipts_across_restart(fresh_db):
    engine, url = fresh_db
    m = "supplier_rt"
    ev = evidence("ev_rt", m, 99000, included=frozenset({"r_in_balance", "r_other"}))
    cap_store.save_evidence(engine, ev)
    cap_store.save_evidence(engine, ev)  # identical replay: idempotent
    assert cap_store.load_evidence(engine, "ev_rt") == ev
    with pytest.raises(EvidenceConflictError):  # same id, different included set
        cap_store.save_evidence(engine, evidence("ev_rt", m, 99000, included=frozenset({"x"})))
    with pytest.raises(EvidenceConflictError):  # same id, other amount
        cap_store.save_evidence(
            engine, evidence("ev_rt", m, 1, included=frozenset({"r_in_balance", "r_other"}))
        )

    engine.dispose()  # "restart": a brand-new engine and connection pool
    restarted = create_engine(url)
    try:
        assert cap_store.load_evidence(restarted, "ev_rt") == ev
        at = T0 + timedelta(hours=1)
        dup = receipt("r_in_balance", m)
        snap = cap_store.capacity_now(restarted, evidence_id="ev_rt", at=at, receipts=[dup, dup])
        assert snap.verified_incoming == Money(0, USD)  # already in the observed balance
        assert snap.deployable == Money(99000, USD)
    finally:
        restarted.dispose()


def test_duplicate_receipts_cannot_fund_two_reservations(fresh_db):
    engine, _ = fresh_db
    m = "supplier_dup"
    cap_store.save_evidence(engine, evidence("ev_dup", m))
    r = receipt("r990", m)
    at = T0 + timedelta(hours=1)
    cap_store.reserve_capacity(
        engine,
        evidence_id="ev_dup",
        commitment=Commitment("c1", m, "plan_1", Money(99000, USD)),
        at=at,
        receipts=[r, r],
    )
    with pytest.raises(InsufficientCapacityError):  # deployable was $990, never $1,980
        cap_store.reserve_capacity(
            engine,
            evidence_id="ev_dup",
            commitment=Commitment("c2", m, "plan_2", Money(99000, USD)),
            at=at,
            receipts=[r, r],
        )
    with pytest.raises(ReceiptConflictError):
        cap_store.capacity_now(
            engine, evidence_id="ev_dup", at=at, receipts=[r, receipt("r990", m, 1)]
        )
    assert cap_store.active_commitment_total(engine, m) == 99000


def test_older_snapshot_cannot_bypass_a_newer_lower_balance(fresh_db):
    engine, _ = fresh_db
    m = "buyer_sup"
    cap_store.save_evidence(engine, evidence("ev_old", m, 149000, 50000))
    cap_store.save_evidence(
        engine, evidence("ev_new", m, 1000, 0, observed=T0 + timedelta(hours=1))
    )
    at = T0 + timedelta(hours=2)
    with pytest.raises(SupersededEvidenceError):
        cap_store.reserve_capacity(
            engine,
            evidence_id="ev_old",
            commitment=Commitment("c_old", m, "plan", Money(90000, USD)),
            at=at,
        )
    with pytest.raises(SupersededEvidenceError):
        cap_store.capacity_now(engine, evidence_id="ev_old", at=at)
    assert cap_store.current_evidence(engine, m, USD, at).evidence_id == "ev_new"
    assert cap_store.active_commitment_total(engine, m) == 0
    # Before the newer observation, the older snapshot was the current one.
    before = cap_store.capacity_now(engine, evidence_id="ev_old", at=T0 + timedelta(minutes=30))
    assert before.deployable == Money(99000, USD)


def test_two_observations_of_one_balance_at_the_same_instant_conflict(fresh_db):
    engine, _ = fresh_db
    cap_store.save_evidence(engine, evidence("ev_a", "m_same", 100))
    with pytest.raises(EvidenceConflictError):
        cap_store.save_evidence(engine, evidence("ev_b", "m_same", 200))


def test_included_receipts_are_immutable_in_the_database(fresh_db):
    engine, _ = fresh_db
    cap_store.save_evidence(engine, evidence("ev_imm", "m_imm", included=frozenset({"r1"})))
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.execute(
            text(
                "UPDATE capacity_evidence SET included_receipt_ids = '{}'"
                " WHERE evidence_id = 'ev_imm'"
            )
        )
    with pytest.raises(DBAPIError, match="included_receipt"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO capacity_evidence (evidence_id, merchant_id, currency,"
                " verified_spendable_cents, protected_reserve_cents, held_cents, pending_cents,"
                " kind, observed_at, valid_until, provenance_source, provenance_reference,"
                " provenance_recorded_at, included_receipt_ids) VALUES ('ev_null', 'm', 'USD',"
                " 0, 0, 0, 0, 'merchant_attested', now(), now() + interval '1 hour',"
                " 'merchant_attested', 'x', now(), ARRAY[NULL]::text[])"
            )
        )


# --- finding 6: atomic settlement import ------------------------------------------------


def test_interrupted_import_books_nothing_and_a_retry_completes(fresh_db, monkeypatch):
    engine, _ = fresh_db
    s = settlement()
    real_post = store._post
    calls = {"n": 0}

    def crash_on_second(conn, entry):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash between bundle entries")
        return real_post(conn, entry)

    monkeypatch.setattr(store, "_post", crash_on_second)
    with pytest.raises(RuntimeError):
        import_settlement(engine, s, ENTITY)
    monkeypatch.undo()
    assert calls["n"] == 2
    assert store.entry_count(engine, "paypal:sandbox:") == 0  # not even the first entry
    assert settlement_import_status(engine, s, ENTITY) is BundleStatus.NOT_IMPORTED

    done = import_settlement(engine, s, ENTITY)
    assert done.status is BundleStatus.COMPLETE
    assert [r.status for r in done.results] == [store.PostStatus.POSTED] * 3
    again = import_settlement(engine, s, ENTITY)
    assert [r.status for r in again.results] == [store.PostStatus.DUPLICATE] * 3
    assert settlement_import_status(engine, s, ENTITY) is BundleStatus.COMPLETE
    assert store.trial_balance(engine) == {"USD": 0, "CAD": 0}


def test_partially_booked_bundle_is_reported_partial_until_completed(fresh_db):
    engine, _ = fresh_db
    s = settlement()
    issued = settlement_entries(s, ENTITY)[0]
    store.post_entry(engine, issued)  # e.g. booked one-by-one before this fix
    assert settlement_import_status(engine, s, ENTITY) is BundleStatus.PARTIAL
    done = import_settlement(engine, s, ENTITY)
    assert [r.status for r in done.results] == [
        store.PostStatus.DUPLICATE,
        store.PostStatus.POSTED,
        store.PostStatus.POSTED,
    ]
    assert done.status is BundleStatus.COMPLETE


def test_bundle_with_conflicting_content_is_never_reported_complete(fresh_db):
    engine, _ = fresh_db
    s = settlement()
    issued = settlement_entries(s, ENTITY)[0]
    tampered = JournalEntry(
        **{**{f: getattr(issued, f) for f in issued.__slots__}, "description": "x"}
    )
    store.post_entry(engine, tampered)
    assert settlement_import_status(engine, s, ENTITY) is BundleStatus.CONFLICT
    with pytest.raises(store.IdempotencyConflictError):
        import_settlement(engine, s, ENTITY)
    assert store.entry_count(engine, "paypal:sandbox:capture:") == 0  # nothing else booked


def test_concurrent_imports_book_the_bundle_once(fresh_db):
    engine, _ = fresh_db
    s = settlement()
    barrier = threading.Barrier(4)
    results, errors = [], []

    def worker():
        barrier.wait()
        try:
            results.append(import_settlement(engine, s, ENTITY))
        except Exception as exc:  # asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    kinds = Counter(tuple(r.status for r in imp.results) for imp in results)
    assert kinds == {
        (store.PostStatus.POSTED,) * 3: 1,
        (store.PostStatus.DUPLICATE,) * 3: 3,
    }
    assert store.entry_count(engine, "paypal:sandbox:") == 3


# --- finding 5: reversal evidence in the database --------------------------------------


def test_local_correction_is_stored_as_manual_adjustment_without_provider_ref(fresh_db):
    engine, _ = fresh_db
    s = settlement()
    import_settlement(engine, s, ENTITY)
    cap = settlement_entries(s, ENTITY)[-1]
    rev = cap.reversal(
        "local:manual:capture-correction",
        cap.occurred_at + timedelta(days=1),
        "booked to the wrong entity",
        evidence_class=EvidenceClass.MANUAL_ADJUSTMENT,
    )
    assert store.post_entry(engine, rev).status is store.PostStatus.POSTED
    with engine.connect() as c:
        row = c.execute(
            select(journal_entry).where(journal_entry.c.idempotency_key == rev.idempotency_key)
        ).one()
        original = c.execute(
            select(journal_entry).where(journal_entry.c.idempotency_key == cap.idempotency_key)
        ).one()
    assert row.evidence_class == "manual_adjustment"
    assert row.provider is None and row.provider_resource_id is None
    assert row.reverses_entry_id == original.id
    assert original.evidence_class == "sandbox_verified"  # the verified payment is preserved


def test_store_refuses_a_synthetic_reversal_of_real_books(fresh_db):
    engine, _ = fresh_db
    s = settlement()
    import_settlement(engine, s, ENTITY)
    cap = settlement_entries(s, ENTITY)[-1]
    hand_built = JournalEntry(
        cap.entity_id,
        "syn:fake:reversal",
        EntryKind.REVERSAL,
        EvidenceClass.SYNTHETIC,
        "synthetic reversal of a real payment",
        cap.occurred_at + timedelta(days=1),
        tuple(Posting(p.account, -p.amount) for p in cap.postings),
        reverses_key=cap.idempotency_key,
    )
    with pytest.raises(store.ReversalError):
        store.post_entry(engine, hand_built)


def test_one_provider_refund_can_back_only_one_reversal(fresh_db):
    engine, _ = fresh_db
    refund = ProviderEventRef("paypal", "sandbox", "refund", "TEST-ONLY-NOT-A-REAL-REFUND")
    originals = [
        JournalEntry(
            "attested_entity",
            f"attested:sale:{i}",
            EntryKind.INVOICE_ISSUED,
            EvidenceClass.MERCHANT_ATTESTED,
            "merchant-attested sale (test)",
            T0,
            (
                Posting("accounts_receivable:x", SignedAmount(100, USD)),
                Posting("revenue:sales", SignedAmount(-100, USD)),
            ),
        )
        for i in (1, 2)
    ]
    for e in originals:
        store.post_entry(engine, e)
    revs = [
        e.reversal(
            f"{e.idempotency_key}:refund",
            T0 + timedelta(hours=1),
            "provider refund",
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            provider_ref=refund,
        )
        for e in originals
    ]
    assert store.post_entry(engine, revs[0]).status is store.PostStatus.POSTED
    with pytest.raises(store.DuplicateProviderEventError):
        store.post_entry(engine, revs[1])


def test_concurrent_identical_provider_entry_replays_are_duplicates_not_errors(fresh_db):
    """Found while fixing finding 6: with ON CONFLICT on the idempotency key only, a
    concurrent replay could hit the provider-event index first and raise instead of
    returning DUPLICATE. Every unique index is now an arbiter."""
    engine, _ = fresh_db
    cap = settlement_entries(settlement(), ENTITY)[-1]
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker():
        barrier.wait()
        try:
            results.append(store.post_entry(engine, cap))
        except Exception as exc:  # asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert Counter(r.status for r in results) == {
        store.PostStatus.POSTED: 1,
        store.PostStatus.DUPLICATE: 7,
    }
    assert store.entry_count(engine, "paypal:sandbox:capture:") == 1
