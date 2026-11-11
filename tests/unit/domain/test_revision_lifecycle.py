"""P1 review finding 4: offer expiry vs. the continuing effect of accepted terms.

- An offer (DRAFT) may carry `expires_at`; after it, the offer can no longer be accepted.
- An acceptance (AUTHORIZED, `supersedes` = the offer) must happen before the deadline,
  must match the offered terms exactly and needs both parties.
- An accepted amendment has no expiry: it stays in force after the original offer
  deadline until later authorized terms replace it.
- A rejected, already-accepted or countered offer is closed and cannot be accepted.
- Payment-authorization / plan expiry is not an invoice property (P2/P4).
"""

from datetime import UTC, datetime, timedelta

import pytest

from api.domain.invoice import (
    ConflictingRevisionError,
    ExpiredRevisionError,
    InvalidInvoiceError,
    InvoiceRecord,
    Obligation,
    ObligationRevision,
    OverSettlementError,
    Provenance,
    RevisionStatus,
    SettlementEvent,
    SettlementKind,
    SettlementStatus,
    SourceKind,
    UnauthorizedConcessionError,
)
from api.domain.money import USD, Money

T0 = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
PROV = Provenance(SourceKind.SYNTHETIC_FIXTURE, "unit-test", T0)
BOTH = frozenset({"buyer", "food_supplier"})
DUE = T0 + timedelta(days=30)
DEADLINE = T0 + timedelta(hours=2)


def usd(c):
    return Money(c, USD)


def h(n):
    return T0 + timedelta(hours=n)


def rev(n, status, at, *, concession=1000, auth=None, **kw):
    if auth is None:
        auth = BOTH if status is RevisionStatus.AUTHORIZED else frozenset({"food_supplier"})
    return ObligationRevision(
        "food_001",
        n,
        status,
        kw.pop("due_at", DUE),
        kw.pop("business_tz", "America/Toronto"),
        usd(concession),
        at,
        PROV,
        authorized_by=auth,
        **kw,
    )


def base() -> InvoiceRecord:
    ob = Obligation("food_001", "buyer", "food_supplier", usd(100000), T0, PROV)
    return InvoiceRecord.open(ob, rev(1, RevisionStatus.AUTHORIZED, T0, concession=0))


def offered() -> InvoiceRecord:
    """Supplier offers $10 off, open until DEADLINE."""
    return base().add_revision(rev(2, RevisionStatus.DRAFT, h(1), expires_at=DEADLINE))


def accept(r: InvoiceRecord, n: int, at: datetime, supersedes: int, **kw) -> InvoiceRecord:
    return r.add_revision(rev(n, RevisionStatus.AUTHORIZED, at, supersedes=supersedes, **kw))


def pay(eid, cents, at, kind=SettlementKind.CASH_APPLIED, reverses=None):
    return SettlementEvent(eid, "food_001", kind, usd(cents), at, PROV, reverses)


# --- acceptance before the deadline, continuing effect after it ------------------------


def test_offer_counts_nothing_until_accepted():
    p = offered().position(h(1.5))
    assert p.authorized_concession == usd(0)
    assert p.revision_no == 1


def test_accepted_amendment_stays_in_force_after_the_offer_deadline():
    r = accept(offered(), 3, h(1.5), 2)
    for at in (h(1.5), DEADLINE, DEADLINE + timedelta(days=60)):
        p = r.position(at)
        assert p.authorized_concession == usd(1000)
        assert p.revision_no == 3
    # Paying $990 long after the offer deadline still settles under the accepted terms.
    paid = r.apply(pay("pay1", 99000, DEADLINE + timedelta(days=10)))
    assert paid.position().status is SettlementStatus.SETTLED


def test_acceptance_at_or_after_the_deadline_is_refused():
    for late in (DEADLINE, DEADLINE + timedelta(seconds=1)):
        with pytest.raises(ExpiredRevisionError):
            accept(offered(), 3, late, 2)


def test_acceptance_needs_both_parties():
    with pytest.raises(UnauthorizedConcessionError):
        accept(offered(), 3, h(1.5), 2, auth=frozenset({"buyer"}))


@pytest.mark.parametrize(
    "change",
    [
        {"concession": 9000},
        {"due_at": DUE + timedelta(days=1)},
        {"business_tz": "UTC"},
    ],
)
def test_acceptance_must_match_the_offered_terms(change):
    with pytest.raises(ConflictingRevisionError, match="offered terms"):
        accept(offered(), 3, h(1.5), 2, **change)


def test_accepted_and_rejected_terms_carry_no_expiry():
    with pytest.raises(InvalidInvoiceError):
        rev(3, RevisionStatus.AUTHORIZED, h(1.5), supersedes=2, expires_at=DEADLINE)
    with pytest.raises(InvalidInvoiceError):
        rev(3, RevisionStatus.REJECTED, h(1.5), supersedes=2, expires_at=DEADLINE)
    with pytest.raises(InvalidInvoiceError):  # an offer must expire after it is made
        rev(2, RevisionStatus.DRAFT, h(1), expires_at=h(1))


# --- closed offers ----------------------------------------------------------------------


def test_rejected_offer_cannot_later_be_accepted():
    r = offered().add_revision(
        rev(3, RevisionStatus.REJECTED, h(1.2), supersedes=2, auth=frozenset({"buyer"}))
    )
    assert r.position().authorized_concession == usd(0)
    with pytest.raises(ConflictingRevisionError):
        accept(r, 4, h(1.5), 2)
    with pytest.raises(ConflictingRevisionError):  # a rejection is not itself acceptable
        accept(r, 4, h(1.5), 3)


def test_offer_can_be_accepted_only_once():
    r = accept(offered(), 3, h(1.5), 2)
    with pytest.raises(ConflictingRevisionError):
        accept(r, 4, h(1.6), 2)
    with pytest.raises(ConflictingRevisionError):  # nor rejected after acceptance
        r.add_revision(rev(4, RevisionStatus.REJECTED, h(1.6), supersedes=2))


def test_countered_offer_is_closed_and_the_counteroffer_can_be_accepted():
    r = offered().add_revision(
        rev(
            3,
            RevisionStatus.DRAFT,
            h(1.2),
            concession=1500,
            supersedes=2,
            auth=frozenset({"buyer"}),
            expires_at=h(3),
        )
    )
    with pytest.raises(ConflictingRevisionError):
        accept(r, 4, h(1.5), 2)
    done = accept(r, 4, h(2.5), 3, concession=1500)  # after the first deadline: fine
    assert done.position().authorized_concession == usd(1500)


def test_rejection_must_reference_an_offer():
    with pytest.raises(InvalidInvoiceError):
        offered().add_revision(rev(3, RevisionStatus.REJECTED, h(1.5)))
    with pytest.raises(ConflictingRevisionError):  # rejecting in-force terms is meaningless
        offered().add_revision(rev(3, RevisionStatus.REJECTED, h(1.5), supersedes=1))


def test_direct_amendment_signed_by_both_is_a_new_agreement_not_an_acceptance():
    """Without `supersedes`, both parties sign fresh terms; no offer deadline applies."""
    late = offered().add_revision(rev(3, RevisionStatus.AUTHORIZED, DEADLINE + timedelta(hours=1)))
    assert late.position().authorized_concession == usd(1000)


# --- interaction with payments and reversals --------------------------------------------


def test_partially_paid_invoice_accepts_offer_only_within_remaining_open():
    r = offered().apply(pay("pay1", 40000, h(1.1)))
    ok = accept(r, 3, h(1.5), 2)
    assert ok.position().remaining_open == usd(59000)
    nearly = offered().apply(pay("pay1", 99500, h(1.1)))
    with pytest.raises(OverSettlementError):
        accept(nearly, 3, h(1.5), 2)
    # The refused acceptance left the offer open: still acceptable once it fits.
    reversed_ = nearly.apply(pay("rev1", 99500, h(1.2), SettlementKind.CASH_REVERSED, "pay1"))
    after = accept(reversed_, 3, h(1.5), 2)
    p = after.position()
    assert (p.cash_applied, p.authorized_concession, p.remaining_open) == (
        usd(0),
        usd(1000),
        usd(99000),
    )


def test_reversal_after_acceptance_reopens_cash_but_keeps_the_accepted_terms():
    r = accept(offered(), 3, h(1.5), 2).apply(pay("pay1", 99000, h(1.6)))
    r = r.apply(
        pay("rev1", 99000, DEADLINE + timedelta(days=1), SettlementKind.CASH_REVERSED, "pay1")
    )
    p = r.position()
    assert p.authorized_concession == usd(1000)  # the amendment is not undone by a refund
    assert p.remaining_open == usd(99000)
    assert p.status is SettlementStatus.OPEN
