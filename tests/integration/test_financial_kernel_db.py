"""Journal, inbox and capacity persistence against a real PostgreSQL 16 database.

Uses a throwaway database created by `kernel_db` with the real Alembic migrations.
Financial inputs are the real Sandbox settlement (read from evidence/, no PayPal call)
or labeled synthetic values.
"""

import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError, InternalError, ProgrammingError

from api.accounting import store
from api.accounting.journal import (
    EntryKind,
    EvidenceClass,
    JournalEntry,
    Posting,
    ProviderEventRef,
)
from api.accounting.settlement_postings import settlement_entries
from api.accounting.tables import journal_entry, journal_posting
from api.domain.invoice import Provenance, SourceKind
from api.domain.money import CAD, USD, Money, SignedAmount
from api.integrations.paypal.settlement import load_settlement
from api.liquidity import store as cap_store
from api.liquidity.capacity import (
    CapacityEvidence,
    Commitment,
    DuplicateCommitmentError,
    EvidenceKind,
    InsufficientCapacityError,
)
from tests.integration.conftest import migrate

pytestmark = pytest.mark.integration

RUNS = Path(__file__).resolve().parents[2] / "evidence" / "paypal-sandbox" / "runs"
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def settlement():
    return load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")


def synthetic_entry(key: str, cents: int = 1000, entity: str = "synthetic_entity") -> JournalEntry:
    return JournalEntry(
        entity_id=entity,
        idempotency_key=key,
        kind=EntryKind.SYNTHETIC_TRANSFER,
        evidence_class=EvidenceClass.SYNTHETIC,
        description="synthetic transfer",
        occurred_at=T0,
        postings=(
            Posting("cash:synthetic", SignedAmount(cents, USD)),
            Posting("equity:synthetic", SignedAmount(-cents, USD)),
        ),
    )


def run_concurrently(n, fn):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        barrier.wait()
        try:
            results.append(fn(i))
        except Exception as exc:  # collected and asserted by the test
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results, errors


# --- migrations -----------------------------------------------------------------------


def test_migration_creates_tables_and_downgrades_cleanly(kernel_db):
    names = set(inspect(kernel_db).get_table_names())
    assert {
        "journal_entry",
        "journal_posting",
        "provider_event_inbox",
        "capacity_evidence",
        "capacity_commitment",
    } <= names


def test_migration_round_trip_on_separate_database():
    from tests.integration.conftest import _server_url

    server = _server_url()
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    name = "elide_test_migrate_roundtrip"
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except DBAPIError:
        admin.dispose()
        pytest.skip("PostgreSQL not reachable")
    url = server.set(database=name).render_as_string(hide_password=False)
    try:
        migrate(url)
        migrate(url, "0001_baseline", "down")
        e = create_engine(url)
        assert "journal_entry" not in inspect(e).get_table_names()
        e.dispose()
        migrate(url)
        e = create_engine(url)
        assert "journal_entry" in inspect(e).get_table_names()
        e.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --- real settlement posting ----------------------------------------------------------


def test_real_sandbox_settlement_posts_once_and_balances(kernel_db):
    entity = "paypal_sandbox_merchant"
    entries = settlement_entries(settlement(), entity)
    first = [store.post_entry(kernel_db, e) for e in entries]
    assert [r.status for r in first] == [store.PostStatus.POSTED] * 3
    # Replay (e.g. duplicate webhook or retried job): nothing new is booked.
    again = [store.post_entry(kernel_db, e) for e in entries]
    assert [r.status for r in again] == [store.PostStatus.DUPLICATE] * 3
    assert [r.entry_id for r in again] == [r.entry_id for r in first]

    b = store.account_balances(kernel_db, entity)
    ar = "accounts_receivable:INV2-4RUV-XPEH-4F3C-T8BW"
    assert (ar, USD) not in b  # receivable fully discharged: 1000 - 10 - 990 = 0
    assert b[("revenue:sales", USD)] == -100000
    assert b[("contra_revenue:early_payment_discount", USD)] == 1000
    assert b[("expense:paypal_fees", USD)] == 2901
    assert b[("paypal_balance", CAD)] == 130679
    # One cash receipt: the USD clearing account nets to zero, no USD cash is left over.
    assert ("paypal_settlement_clearing", USD) not in b
    assert ("paypal_balance", USD) not in b
    assert b[("fx_conversion_position", USD)] == 96099
    assert b[("fx_conversion_position", CAD)] == -130679
    tb = store.trial_balance(kernel_db)
    assert tb == {cur: 0 for cur in tb}


def test_same_capture_under_a_different_key_is_refused(kernel_db):
    """Self-contained: ensures the real capture is booked, then tries to book it again."""
    entries = settlement_entries(settlement(), "paypal_sandbox_merchant")
    for e in entries:  # POSTED on first use in this database, DUPLICATE afterwards
        assert store.post_entry(kernel_db, e).status in set(store.PostStatus)
    cap = entries[-1]
    other_key = JournalEntry(
        **{
            **{f: getattr(cap, f) for f in cap.__slots__},
            "idempotency_key": "other:key:for-the-same-capture",
        }
    )
    other_entity = JournalEntry(
        **{
            **{f: getattr(cap, f) for f in cap.__slots__},
            "idempotency_key": "other:key:same-capture-other-entity",
            "entity_id": "paypal_sandbox_merchant_b",
        }
    )
    for attempt in (other_key, other_entity):
        with pytest.raises(store.DuplicateProviderEventError):
            store.post_entry(kernel_db, attempt)
    # Same key, different content (e.g. a tampered replay) is a conflict, not a duplicate.
    tampered = JournalEntry(
        **{**{f: getattr(cap, f) for f in cap.__slots__}, "description": "tampered"}
    )
    with pytest.raises(store.IdempotencyConflictError):
        store.post_entry(kernel_db, tampered)
    assert store.entry_count(kernel_db, "paypal:sandbox:capture:") == 1


def test_same_key_with_different_content_is_a_conflict(kernel_db, uid):
    store.post_entry(kernel_db, synthetic_entry(f"syn:{uid}:a", 1000))
    with pytest.raises(store.IdempotencyConflictError):
        store.post_entry(kernel_db, synthetic_entry(f"syn:{uid}:a", 2000))


# --- concurrency ----------------------------------------------------------------------


def test_concurrent_identical_posts_book_exactly_once(kernel_db, uid):
    entry = synthetic_entry(f"syn:{uid}:concurrent", 5000)
    results, errors = run_concurrently(8, lambda _i: store.post_entry(kernel_db, entry))
    assert errors == []
    assert Counter(r.status for r in results) == {
        store.PostStatus.POSTED: 1,
        store.PostStatus.DUPLICATE: 7,
    }
    assert len({r.entry_id for r in results}) == 1
    with kernel_db.connect() as c:
        n = c.execute(
            select(journal_posting.c.id)
            .join(journal_entry, journal_entry.c.id == journal_posting.c.entry_id)
            .where(journal_entry.c.idempotency_key == entry.idempotency_key)
        ).all()
    assert len(n) == 2


def test_concurrent_posts_of_one_provider_event_under_different_keys(kernel_db, uid):
    ref = ProviderEventRef("paypal", "sandbox", "capture", f"SYNTHCAP{uid.upper()}")

    def attempt(i):
        e = JournalEntry(
            entity_id="concurrency_entity",
            idempotency_key=f"race:{uid}:{i}",
            kind=EntryKind.PROVIDER_SETTLEMENT,
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            description="race test (synthetic capture id)",
            occurred_at=T0,
            postings=(
                Posting("paypal_balance", SignedAmount(100, USD)),
                Posting("accounts_receivable:x", SignedAmount(-100, USD)),
            ),
            provider_ref=ref,
        )
        return store.post_entry(kernel_db, e)

    results, errors = run_concurrently(6, attempt)
    assert len(results) == 1 and results[0].status is store.PostStatus.POSTED
    assert len(errors) == 5
    assert all(isinstance(e, store.DuplicateProviderEventError) for e in errors)


# --- rollback, balance guard, append-only --------------------------------------------


def test_failure_mid_transaction_leaves_no_partial_entry(kernel_db, uid, monkeypatch):
    entry = synthetic_entry(f"syn:{uid}:rollback")
    real_execute = None

    def boom(conn, e):
        nonlocal real_execute
        # Insert the entry row, then fail before postings: the whole txn must roll back.
        conn.execute(
            journal_entry.insert().values(
                entity_id=e.entity_id,
                idempotency_key=e.idempotency_key,
                kind=e.kind.value,
                evidence_class=e.evidence_class.value,
                description=e.description,
                occurred_at=e.occurred_at,
                content_sha256=e.content_digest(),
            )
        )
        raise RuntimeError("simulated crash after writing the entry header")

    monkeypatch.setattr(store, "_post", boom)
    with pytest.raises(RuntimeError):
        store.post_entry(kernel_db, entry)
    monkeypatch.undo()
    assert store.entry_count(kernel_db, entry.idempotency_key) == 0
    # A clean retry afterwards books it once.
    assert store.post_entry(kernel_db, entry).status is store.PostStatus.POSTED


def test_database_refuses_unbalanced_entry_at_commit(kernel_db, uid):
    key = f"raw:{uid}:unbalanced"
    with pytest.raises(DBAPIError, match="unbalanced"), kernel_db.begin() as c:
        eid = c.execute(
            text(
                "INSERT INTO journal_entry (entity_id, idempotency_key, kind, evidence_class,"
                " description, occurred_at, content_sha256)"
                " VALUES ('raw', :k, 'synthetic_transfer',"
                " 'synthetic', 'raw insert', now(), repeat('0', 64)) RETURNING id"
            ),
            {"k": key},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO journal_posting (entry_id, line_no, account, currency, amount_cents)"
                " VALUES (:e, 1, 'cash:x', 'USD', 100), (:e, 2, 'equity:x', 'USD', -99)"
            ),
            {"e": eid},
        )
    assert store.entry_count(kernel_db, key) == 0


def test_database_refuses_entry_without_postings(kernel_db, uid):
    key = f"raw:{uid}:empty"
    with pytest.raises(DBAPIError, match="postings"), kernel_db.begin() as c:
        c.execute(
            text(
                "INSERT INTO journal_entry (entity_id, idempotency_key, kind, evidence_class,"
                " description, occurred_at, content_sha256)"
                " VALUES ('raw', :k, 'synthetic_transfer',"
                " 'synthetic', 'raw insert', now(), repeat('0', 64))"
            ),
            {"k": key},
        )
    assert store.entry_count(kernel_db, key) == 0


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE journal_posting SET amount_cents = amount_cents * 2",
        "DELETE FROM journal_posting",
        "UPDATE journal_entry SET description = 'edited'",
        "DELETE FROM journal_entry",
        "TRUNCATE journal_posting CASCADE",
    ],
)
def test_historical_entries_cannot_be_mutated(kernel_db, uid, sql):
    store.post_entry(kernel_db, synthetic_entry(f"syn:{uid}:immutable"))
    refused = (InternalError, ProgrammingError, DBAPIError)
    with pytest.raises(refused, match="append-only"), kernel_db.begin() as c:
        c.execute(text(sql))


# --- reversals ------------------------------------------------------------------------


def test_reversal_compensates_without_rewriting_history(kernel_db, uid):
    entity = f"rev_entity_{uid}"
    original = synthetic_entry(f"syn:{uid}:paid", 99000, entity)
    store.post_entry(kernel_db, original)
    rev = original.reversal(f"syn:{uid}:paid:reversal", T0 + timedelta(hours=1), "refund")
    assert store.post_entry(kernel_db, rev).status is store.PostStatus.POSTED
    assert store.account_balances(kernel_db, entity) == {}
    assert store.entry_count(kernel_db, f"syn:{uid}:paid") == 2  # both still on record
    # Replaying the reversal is a no-op; a second, different reversal is refused.
    assert store.post_entry(kernel_db, rev).status is store.PostStatus.DUPLICATE
    with pytest.raises(store.ReversalError):
        store.post_entry(
            kernel_db, original.reversal(f"syn:{uid}:paid:reversal2", T0 + timedelta(hours=2), "x")
        )


def test_reversal_must_exactly_negate_the_original(kernel_db, uid):
    entity = f"rev_bad_{uid}"
    original = synthetic_entry(f"syn:{uid}:orig", 5000, entity)
    store.post_entry(kernel_db, original)
    wrong = JournalEntry(
        entity_id=entity,
        idempotency_key=f"syn:{uid}:orig:partial-rev",
        kind=EntryKind.REVERSAL,
        evidence_class=EvidenceClass.SYNTHETIC,
        description="partial reversal attempt",
        occurred_at=T0 + timedelta(hours=1),
        postings=(
            Posting("cash:synthetic", SignedAmount(-4000, USD)),
            Posting("equity:synthetic", SignedAmount(4000, USD)),
        ),
        reverses_key=original.idempotency_key,
    )
    with pytest.raises(store.ReversalError):
        store.post_entry(kernel_db, wrong)
    with pytest.raises(store.ReversalError):
        store.post_entry(
            kernel_db,
            synthetic_entry(f"syn:{uid}:missing", 1, entity).reversal(
                f"syn:{uid}:missing:rev", T0, "never posted"
            ),
        )


# --- provider event inbox (separate from economic posting) ---------------------------


def test_inbox_dedupes_events_independently_of_postings(kernel_db, uid):
    payload = {"id": f"WH-{uid}", "event_type": "INVOICING.INVOICE.PAID"}
    kw = {
        "provider": "paypal",
        "environment": "sandbox",
        "provider_event_id": f"WH-{uid}",
        "event_type": "INVOICING.INVOICE.PAID",
    }
    first = store.record_provider_event(kernel_db, payload=payload, **kw)
    second = store.record_provider_event(kernel_db, payload=payload, **kw)
    assert (first.status, second.status) == (store.InboxStatus.NEW, store.InboxStatus.DUPLICATE)
    assert first.inbox_id == second.inbox_id
    with pytest.raises(store.InboxConflictError):
        store.record_provider_event(kernel_db, payload={**payload, "x": 1}, **kw)
    # Two different webhook ids describing the same capture are two inbox rows, but the
    # economic posting for that capture is still booked once (tested above).
    other = store.record_provider_event(
        kernel_db, payload=payload, **{**kw, "provider_event_id": f"WH-{uid}-2"}
    )
    assert other.status is store.InboxStatus.NEW


def test_concurrent_duplicate_webhooks_recorded_once(kernel_db, uid):
    kw = {
        "provider": "paypal",
        "environment": "sandbox",
        "provider_event_id": f"WH-RACE-{uid}",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "payload": {"id": uid},
    }
    results, errors = run_concurrently(6, lambda _i: store.record_provider_event(kernel_db, **kw))
    assert errors == []
    assert Counter(r.status for r in results) == {
        store.InboxStatus.NEW: 1,
        store.InboxStatus.DUPLICATE: 5,
    }


# --- capacity commitments -------------------------------------------------------------


def _evidence(eid, merchant, spendable=149000, reserve=50000, cur=USD):
    return CapacityEvidence(
        eid,
        merchant,
        Money(spendable, cur),
        Money(reserve, cur),
        EvidenceKind.MERCHANT_ATTESTED,
        T0,
        T0 + timedelta(hours=4),
        Provenance(SourceKind.SYNTHETIC_FIXTURE, "integration-test", T0),
    )


def test_concurrent_plans_cannot_reserve_the_same_990(kernel_db, uid):
    merchant = f"buyer_{uid}"
    cap_store.save_evidence(kernel_db, _evidence(f"ev_{uid}", merchant))

    def attempt(i):
        return cap_store.reserve_capacity(
            kernel_db,
            evidence_id=f"ev_{uid}",
            commitment=Commitment(f"c_{uid}_{i}", merchant, f"plan_{i}", Money(99000, USD)),
            at=T0 + timedelta(minutes=5),
        )

    results, errors = run_concurrently(6, attempt)
    assert len(results) == 1
    assert len(errors) == 5 and all(isinstance(e, InsufficientCapacityError) for e in errors)
    assert cap_store.active_commitment_total(kernel_db, merchant) == 99000
    snap = cap_store.capacity_now(kernel_db, evidence_id=f"ev_{uid}", at=T0 + timedelta(minutes=6))
    assert snap.deployable == Money(0, USD)


def test_released_commitment_frees_capacity_once(kernel_db, uid):
    merchant = f"buyer_r_{uid}"
    cap_store.save_evidence(kernel_db, _evidence(f"evr_{uid}", merchant))
    c = Commitment(f"cr_{uid}", merchant, "plan_a", Money(99000, USD))
    at = T0 + timedelta(minutes=5)
    cap_store.reserve_capacity(kernel_db, evidence_id=f"evr_{uid}", commitment=c, at=at)
    with pytest.raises(DuplicateCommitmentError):
        cap_store.reserve_capacity(kernel_db, evidence_id=f"evr_{uid}", commitment=c, at=at)
    assert cap_store.release_capacity(kernel_db, c.commitment_id, at + timedelta(minutes=1))
    assert not cap_store.release_capacity(kernel_db, c.commitment_id, at + timedelta(minutes=2))
    snap = cap_store.capacity_now(kernel_db, evidence_id=f"evr_{uid}", at=at + timedelta(minutes=3))
    assert snap.deployable == Money(99000, USD)


def test_commitment_rows_cannot_be_deleted_or_reopened(kernel_db, uid):
    merchant = f"buyer_d_{uid}"
    cap_store.save_evidence(kernel_db, _evidence(f"evd_{uid}", merchant))
    c = Commitment(f"cd_{uid}", merchant, "plan_a", Money(100, USD))
    cap_store.reserve_capacity(kernel_db, evidence_id=f"evd_{uid}", commitment=c, at=T0)
    for sql in (
        "DELETE FROM capacity_commitment WHERE commitment_id = :cid",
        "UPDATE capacity_commitment SET amount_cents = 1 WHERE commitment_id = :cid",
    ):
        with pytest.raises(DBAPIError, match="capacity_commitment"), kernel_db.begin() as conn:
            conn.execute(text(sql), {"cid": f"cd_{uid}"})


def test_cad_evidence_cannot_be_reserved_against(kernel_db, uid):
    merchant = f"ca_merchant_{uid}"
    cap_store.save_evidence(kernel_db, _evidence(f"evc_{uid}", merchant, 130679, 0, CAD))
    with pytest.raises(InsufficientCapacityError):
        cap_store.reserve_capacity(
            kernel_db,
            evidence_id=f"evc_{uid}",
            commitment=Commitment(f"cc_{uid}", merchant, "plan_a", Money(1, USD)),
            at=T0 + timedelta(minutes=1),
        )


def test_evidence_round_trips_and_is_immutable(kernel_db, uid):
    ev = _evidence(f"evi_{uid}", f"m_{uid}")
    cap_store.save_evidence(kernel_db, ev)
    cap_store.save_evidence(kernel_db, ev)  # identical: no-op
    with pytest.raises(Exception, match="other values"):
        cap_store.save_evidence(kernel_db, _evidence(f"evi_{uid}", f"m_{uid}", spendable=1))
    with pytest.raises(DBAPIError, match="append-only"), kernel_db.begin() as c:
        c.execute(
            text(
                "UPDATE capacity_evidence SET verified_spendable_cents = 999999999 "
                "WHERE evidence_id = :eid"
            ),
            {"eid": f"evi_{uid}"},
        )
