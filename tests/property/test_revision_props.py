"""Property test (P1 review finding 4): whatever sequence of offers, counteroffers,
acceptances, rejections and payments is attempted, every accepted term set was an open,
unexpired offer accepted once with identical terms, and accepted terms stay in force."""

from collections import Counter
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from api.domain.invoice import (
    InvoiceError,
    InvoiceRecord,
    Obligation,
    ObligationRevision,
    Provenance,
    RevisionStatus,
    SettlementEvent,
    SettlementKind,
    SourceKind,
)
from api.domain.money import USD, Money, MoneyError

T0 = datetime(2026, 11, 1, tzinfo=UTC)
PROV = Provenance(SourceKind.SYNTHETIC_FIXTURE, "property-test", T0)
BOTH = frozenset({"buyer", "seller"})
DUE = T0 + timedelta(days=30)
FACE = 100_000

step = st.one_of(
    st.tuples(st.just("offer"), st.integers(0, 5000), st.integers(1, 30), st.none()),
    st.tuples(st.just("counter"), st.integers(0, 5000), st.integers(1, 30), st.integers(0, 20)),
    st.tuples(st.just("accept"), st.integers(0, 5000), st.integers(0, 1), st.integers(0, 20)),
    st.tuples(st.just("reject"), st.just(0), st.just(0), st.integers(0, 20)),
    st.tuples(st.just("pay"), st.integers(1, 120_000), st.just(0), st.none()),
    st.tuples(st.just("wait"), st.integers(1, 40), st.just(0), st.none()),
)


def revision(n, status, at, concession, *, supersedes=None, expires=None, auth=None):
    return ObligationRevision(
        "inv",
        n,
        status,
        DUE,
        "UTC",
        Money(concession, USD),
        at,
        PROV,
        expires_at=expires,
        authorized_by=auth if auth is not None else BOTH,
        supersedes=supersedes,
    )


@settings(max_examples=400)
@given(st.lists(step, max_size=30))
def test_accepted_terms_always_come_from_one_open_offer(steps):
    ob = Obligation("inv", "buyer", "seller", Money(FACE, USD), T0, PROV)
    record = InvoiceRecord.open(ob, revision(1, RevisionStatus.AUTHORIZED, T0, 0))
    clock = T0
    for i, (kind, amount, x, target) in enumerate(steps):
        clock += timedelta(minutes=1)
        n = len(record.revisions) + 1
        target_no = None if target is None else 1 + target % len(record.revisions)
        before = record
        try:
            if kind in ("offer", "counter"):
                record = record.add_revision(
                    revision(
                        n,
                        RevisionStatus.DRAFT,
                        clock,
                        amount,
                        supersedes=target_no if kind == "counter" else None,
                        expires=clock + timedelta(minutes=x),
                        auth=frozenset({"seller"}),
                    )
                )
            elif kind == "accept":
                prior = record.revisions[target_no - 1]
                # x == 0: accept the offered terms; x == 1: try different terms.
                c = prior.concession.cents if x == 0 else amount
                record = record.add_revision(
                    revision(n, RevisionStatus.AUTHORIZED, clock, c, supersedes=target_no)
                )
            elif kind == "reject":
                record = record.add_revision(
                    revision(
                        n,
                        RevisionStatus.REJECTED,
                        clock,
                        record.revisions[target_no - 1].concession.cents,
                        supersedes=target_no,
                        auth=frozenset({"buyer"}),
                    )
                )
            elif kind == "pay":
                record = record.apply(
                    SettlementEvent(
                        f"p{i}", "inv", SettlementKind.CASH_APPLIED, Money(amount, USD), clock, PROV
                    )
                )
            else:
                clock += timedelta(minutes=amount)
        except (InvoiceError, MoneyError):
            assert record is before  # refused changes leave no trace

        revs = {r.revision_no: r for r in record.revisions}
        closers = Counter(r.supersedes for r in record.revisions if r.supersedes is not None)
        for r in record.revisions:
            if r.supersedes is None:
                continue
            prior = revs[r.supersedes]
            if prior.status is RevisionStatus.DRAFT:
                assert closers[prior.revision_no] == 1  # each offer is closed at most once
            if r.status is RevisionStatus.AUTHORIZED and prior.status is RevisionStatus.DRAFT:
                assert prior.expires_at is None or r.created_at < prior.expires_at
                assert (r.concession, r.due_at, r.business_tz) == (
                    prior.concession,
                    prior.due_at,
                    prior.business_tz,
                )
            if r.status is RevisionStatus.REJECTED:
                assert prior.status is RevisionStatus.DRAFT
            assert prior.status is not RevisionStatus.REJECTED
        for r in record.revisions:
            if r.status is not RevisionStatus.DRAFT:
                assert r.expires_at is None

        latest_authorized = [r for r in record.revisions if r.status is RevisionStatus.AUTHORIZED]
        p = record.position(clock + timedelta(days=365))  # long after every deadline
        assert p.authorized_concession == latest_authorized[-1].concession
        assert p.cash_applied + p.authorized_concession + p.remaining_open == p.original_open
