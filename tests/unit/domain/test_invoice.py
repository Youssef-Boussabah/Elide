"""Invoice obligations, revisions and settlement events (synthetic values)."""

from datetime import UTC, datetime, timedelta

import pytest

from api.domain.invoice import (
    ConflictingRevisionError,
    DisputeStatus,
    DuplicateEventError,
    DuplicateObligationError,
    ExpiredRevisionError,
    InvalidInvoiceError,
    InvalidReversalError,
    InvoiceRecord,
    Obligation,
    ObligationRegistry,
    ObligationRevision,
    OutOfOrderError,
    OverSettlementError,
    Provenance,
    ProviderRef,
    RevisionStatus,
    SettlementEvent,
    SettlementKind,
    SettlementStatus,
    SourceKind,
    UnauthorizedConcessionError,
)
from api.domain.money import CAD, USD, CurrencyMismatchError, Money

T0 = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
PROV = Provenance(SourceKind.SYNTHETIC_FIXTURE, "unit-test", T0)


def usd(c: int) -> Money:
    return Money(c, USD)


def obligation(oid="food_001", face=100000, ref=None) -> Obligation:
    return Obligation(oid, "buyer", "food_supplier", usd(face), T0, PROV, ref)


def rev(n, *, concession=0, status=RevisionStatus.AUTHORIZED, at=T0, auth=None, **kw):
    if auth is None:
        auth = (
            frozenset({"buyer", "food_supplier"})
            if status is RevisionStatus.AUTHORIZED
            else frozenset()
        )
    return ObligationRevision(
        obligation_id=kw.pop("oid", "food_001"),
        revision_no=n,
        status=status,
        due_at=kw.pop("due_at", T0 + timedelta(days=30)),
        business_tz="America/Toronto",
        concession=usd(concession),
        created_at=at,
        provenance=PROV,
        authorized_by=auth,
        **kw,
    )


def pay(eid, cents, at=T0, kind=SettlementKind.CASH_APPLIED, reverses=None) -> SettlementEvent:
    return SettlementEvent(eid, "food_001", kind, usd(cents), at, PROV, reverses)


def record() -> InvoiceRecord:
    return InvoiceRecord.open(obligation(), rev(1))


def with_discount(concession=1000, at=T0 + timedelta(hours=1)) -> InvoiceRecord:
    return record().add_revision(rev(2, concession=concession, at=at))


# --- original identity --------------------------------------------------------------------


def test_new_invoice_is_fully_open():
    p = record().position()
    assert (p.original_open, p.cash_applied, p.authorized_concession, p.remaining_open) == (
        usd(100000),
        usd(0),
        usd(0),
        usd(100000),
    )
    assert p.status is SettlementStatus.OPEN


def test_original_identity_and_face_survive_revisions():
    r = with_discount()
    assert r.obligation == obligation()
    assert r.position().original_open == usd(100000)
    assert [x.revision_no for x in r.revisions] == [1, 2]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"debtor": "same", "creditor": "same"},
        {"face": Money(0, USD)},
        {"obligation_id": "Has Spaces"},
        {"issued_at": datetime(2026, 11, 1)},  # naive
    ],
)
def test_malformed_obligations_rejected(kwargs):
    base = {
        "obligation_id": "x1",
        "debtor": "a",
        "creditor": "b",
        "face": usd(100),
        "issued_at": T0,
        "provenance": PROV,
    }
    base.update(kwargs)
    with pytest.raises(InvalidInvoiceError):
        Obligation(**base)


def test_revision_one_must_be_original_terms():
    with pytest.raises(InvalidInvoiceError):
        InvoiceRecord.open(obligation(), rev(1, concession=1000))
    with pytest.raises(InvalidInvoiceError):
        InvoiceRecord.open(obligation(), rev(1, status=RevisionStatus.DRAFT))


def test_bad_timezone_rejected():
    with pytest.raises(InvalidInvoiceError):
        ObligationRevision(
            "food_001", 1, RevisionStatus.AUTHORIZED, T0, "Mars/Olympus", usd(0), T0, PROV
        )


# --- concessions --------------------------------------------------------------------------


def test_authorized_discount_counts_toward_settlement_identity():
    p = with_discount().position()
    assert p.authorized_concession == usd(1000)
    assert p.remaining_open == usd(99000)


def test_draft_discount_does_not_count():
    r = record().add_revision(rev(2, concession=1000, status=RevisionStatus.DRAFT))
    assert r.position().authorized_concession == usd(0)
    assert r.position().remaining_open == usd(100000)


@pytest.mark.parametrize("auth", [frozenset(), frozenset({"buyer"}), frozenset({"food_supplier"})])
def test_one_party_cannot_authorize_a_concession(auth):
    with pytest.raises(UnauthorizedConcessionError):
        record().add_revision(rev(2, concession=1000, auth=auth))


def test_third_party_cannot_sign_terms():
    with pytest.raises(UnauthorizedConcessionError):
        record().add_revision(
            rev(2, concession=1000, status=RevisionStatus.DRAFT, auth=frozenset({"carrier"}))
        )


def test_expired_draft_cannot_be_authorized():
    draft_at = T0 + timedelta(hours=1)
    r = record().add_revision(
        rev(
            2,
            concession=1000,
            status=RevisionStatus.DRAFT,
            at=draft_at,
            expires_at=draft_at + timedelta(hours=1),
        )
    )
    late = draft_at + timedelta(hours=2)
    with pytest.raises(ExpiredRevisionError):
        r.add_revision(rev(3, concession=1000, at=late, supersedes=2))
    on_time = r.add_revision(
        rev(3, concession=1000, at=draft_at + timedelta(minutes=30), supersedes=2)
    )
    assert on_time.position().authorized_concession == usd(1000)


def test_concession_cannot_exceed_face_or_change_currency():
    with pytest.raises(OverSettlementError):
        record().add_revision(rev(2, concession=100001))
    bad = ObligationRevision(
        "food_001",
        2,
        RevisionStatus.AUTHORIZED,
        T0,
        "UTC",
        Money(1000, CAD),
        T0,
        PROV,
        authorized_by=frozenset({"buyer", "food_supplier"}),
    )
    with pytest.raises(CurrencyMismatchError):
        record().add_revision(bad)


def test_conflicting_and_skipped_revisions_rejected_identical_replay_ignored():
    r = with_discount()
    assert r.add_revision(rev(2, concession=1000, at=T0 + timedelta(hours=1))) is r
    with pytest.raises(ConflictingRevisionError):
        r.add_revision(rev(2, concession=500, at=T0 + timedelta(hours=1)))
    with pytest.raises(ConflictingRevisionError):
        r.add_revision(rev(4, at=T0 + timedelta(hours=2)))


def test_backdated_revision_rejected():
    r = with_discount(at=T0 + timedelta(hours=2))
    with pytest.raises(OutOfOrderError):
        r.add_revision(rev(3, at=T0 + timedelta(hours=1)))


# --- cash, partials, reversals -----------------------------------------------------------


def test_early_payment_with_authorized_discount_settles_invoice():
    r = with_discount().apply(pay("pay1", 99000, T0 + timedelta(hours=2)))
    p = r.position()
    assert p.status is SettlementStatus.SETTLED
    assert (p.cash_applied, p.authorized_concession, p.remaining_open) == (
        usd(99000),
        usd(1000),
        usd(0),
    )


def test_990_without_authorized_discount_leaves_10_open():
    p = record().apply(pay("pay1", 99000, T0 + timedelta(hours=2))).position()
    assert p.status is SettlementStatus.PARTIALLY_SETTLED
    assert p.remaining_open == usd(1000)


def test_partial_payments_accumulate():
    r = record().apply(pay("p1", 40000)).apply(pay("p2", 60000))
    assert r.position().status is SettlementStatus.SETTLED


def test_overpayment_is_refused_not_absorbed():
    r = with_discount().apply(pay("pay1", 99000, T0 + timedelta(hours=2)))
    with pytest.raises(OverSettlementError):
        r.apply(pay("pay2", 1, T0 + timedelta(hours=3)))


def test_concession_after_full_payment_cannot_overdischarge():
    r = record().apply(pay("pay1", 100000, T0 + timedelta(hours=1)))
    with pytest.raises(OverSettlementError):
        r.add_revision(rev(2, concession=1000, at=T0 + timedelta(hours=2)))


def test_duplicate_payment_event_is_counted_once():
    r = record().apply(pay("pay1", 40000))
    assert r.apply(pay("pay1", 40000)) is r
    assert r.position().cash_applied == usd(40000)
    with pytest.raises(DuplicateEventError):
        r.apply(pay("pay1", 50000))


def test_reversal_reopens_without_erasing_history():
    t1, t2 = T0 + timedelta(hours=2), T0 + timedelta(hours=3)
    paid = with_discount().apply(pay("pay1", 99000, t1))
    reversed_ = paid.apply(pay("rev1", 99000, t2, SettlementKind.CASH_REVERSED, "pay1"))
    assert reversed_.position().remaining_open == usd(99000)
    assert [e.event_id for e in reversed_.events] == ["pay1", "rev1"]
    # The earlier position is still reconstructible: history was not rewritten.
    assert reversed_.position(at=t1).status is SettlementStatus.SETTLED


@pytest.mark.parametrize(
    ("reverses", "cents"),
    [("missing", 100), ("pay1", 40001)],
)
def test_invalid_reversals_rejected(reverses, cents):
    r = record().apply(pay("pay1", 40000))
    with pytest.raises(InvalidReversalError):
        r.apply(pay("rev1", cents, kind=SettlementKind.CASH_REVERSED, reverses=reverses))


def test_cannot_reverse_twice_beyond_original_and_cannot_reverse_a_reversal():
    r = record().apply(pay("pay1", 40000))
    r = r.apply(pay("rev1", 30000, kind=SettlementKind.CASH_REVERSED, reverses="pay1"))
    with pytest.raises(InvalidReversalError):
        r.apply(pay("rev2", 10001, kind=SettlementKind.CASH_REVERSED, reverses="pay1"))
    with pytest.raises(InvalidReversalError):
        r.apply(pay("rev3", 100, kind=SettlementKind.CASH_REVERSED, reverses="rev1"))


def test_wrong_currency_event_rejected():
    ev = SettlementEvent("p1", "food_001", SettlementKind.CASH_APPLIED, Money(100, CAD), T0, PROV)
    with pytest.raises(CurrencyMismatchError):
        record().apply(ev)


def test_dispute_is_reported_on_position():
    r = record().add_revision(rev(2, at=T0 + timedelta(hours=1), dispute=DisputeStatus.OPEN))
    assert r.position().dispute is DisputeStatus.OPEN


# --- duplicate original invoices ---------------------------------------------------------


def test_same_invoice_imported_twice_is_counted_once():
    reg = ObligationRegistry()
    reg.register(obligation())
    reg.register(obligation())
    assert len(reg) == 1
    assert reg.total_face(USD) == usd(100000)


def test_same_id_with_different_terms_rejected():
    reg = ObligationRegistry()
    reg.register(obligation())
    with pytest.raises(DuplicateObligationError):
        reg.register(obligation(face=99000))


def test_same_provider_invoice_under_two_ids_rejected():
    ref = ProviderRef("paypal", "sandbox", "INV2-TEST")
    reg = ObligationRegistry()
    reg.register(obligation("food_001", ref=ref))
    with pytest.raises(DuplicateObligationError):
        reg.register(obligation("food_002", ref=ref))
