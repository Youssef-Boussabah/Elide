"""Liquidity capacity engine (pure, deterministic; no database or provider access).

    raw_capacity = verified_spendable + verified_incoming - protected_reserve - committed
    deployable   = max(0, raw_capacity)        deficit = max(0, -raw_capacity)

Rules (doc 04/05, D006, D008):
- Only USD evidence can produce deployable capacity. Other currencies are reported as
  CURRENCY_BLOCKED with zero deployable USD.
- Evidence counts only inside its validity window [observed_at, valid_until). Stale,
  future-dated, unknown-kind and (unless simulating) synthetic evidence deploy zero.
- Held, pending, promised and provider-confirmed (e.g. PayPal `PAID`) receipts add zero.
  A receipt adds capacity only if it is AVAILABLE_VERIFIED **with separate availability
  evidence** (`AvailabilityEvidence`), usable strictly before the instant being evaluated
  (no same-slot reuse), and not already included in the evidence's observed balance.
  The status value alone proves nothing: synthetic availability counts only in explicit
  simulations, a merchant can attest only its own funds, and the availability evidence
  must be a different artifact from the payment confirmation that created the receipt.
- One economic receipt counts at most once. Receipts are keyed by (merchant, receipt_id);
  identical copies collapse, and two different records for the same receipt (other
  amount, currency, status or evidence) raise `ReceiptConflictError`: the caller must
  supply the receipt's current state once (a held -> verified transition replaces the
  record, it never adds a second receipt).
- Every active commitment of the merchant is subtracted once; a duplicate commitment id
  is an error, so two plans cannot count the same cash twice.
- A negative raw capacity is reported as a deficit, never hidden by the max().
- Evidence supersession (`select_current_evidence`): for one merchant and currency, the
  latest observation at or before the evaluated instant is authoritative. An older
  snapshot is superseded even if its validity window is still open, so a caller cannot
  bypass a newer, lower balance by naming an older evidence id.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from api.domain.invoice import Provenance, SourceKind, check_aware, check_id
from api.domain.money import (
    SPENDABLE_CURRENCIES,
    USD,
    CurrencyMismatchError,
    Money,
)


class CapacityError(ValueError):
    pass


class DuplicateCommitmentError(CapacityError):
    pass


class InsufficientCapacityError(CapacityError):
    pass


class ReceiptConflictError(CapacityError):
    """The same receipt was supplied twice with different content."""


class EvidenceConflictError(CapacityError):
    """Two different evidence records claim the same identity or observation instant."""


class SupersededEvidenceError(CapacityError):
    """A newer observation of the same balance exists; the older one may not be used."""


class EvidenceKind(StrEnum):
    PROVIDER_VERIFIED = "provider_verified"
    MERCHANT_ATTESTED = "merchant_attested"
    SYNTHETIC = "synthetic"  # usable only in explicitly labeled simulations
    UNKNOWN = "unknown"


# Which provenance sources may back each evidence kind. Synthetic data is only ever
# SYNTHETIC; a merchant-entered (unattested) figure can only be UNKNOWN.
_EVIDENCE_SOURCES: dict[EvidenceKind, frozenset[SourceKind]] = {
    EvidenceKind.PROVIDER_VERIFIED: frozenset({SourceKind.PAYPAL_SANDBOX}),
    EvidenceKind.MERCHANT_ATTESTED: frozenset({SourceKind.MERCHANT_ATTESTED}),
    EvidenceKind.SYNTHETIC: frozenset({SourceKind.SYNTHETIC_FIXTURE}),
    EvidenceKind.UNKNOWN: frozenset(
        {SourceKind.MERCHANT_ENTERED, SourceKind.MERCHANT_ATTESTED, SourceKind.PAYPAL_SANDBOX}
    ),
}


class ReceiptStatus(StrEnum):
    PROMISED = "promised"  # invoice or promise, nothing received
    PROVIDER_CONFIRMED = "provider_confirmed"  # e.g. PayPal invoice PAID (D006: not usable)
    HELD = "held"
    AVAILABLE_VERIFIED = "available_verified"  # needs AvailabilityEvidence to count


class AvailabilityBasis(StrEnum):
    """Why a receipt is considered usable. Never derived from a payment status."""

    SYNTHETIC = "synthetic"  # simulation only (allow_synthetic=True)
    MERCHANT_ATTESTED = "merchant_attested"  # the receiving merchant attests its own funds
    PROVIDER_BALANCE = "provider_balance"  # independently fetched available-balance check


class CapacityStatus(StrEnum):
    AVAILABLE = "available"
    DEFICIT = "deficit"
    STALE = "stale"
    NOT_YET_OBSERVED = "not_yet_observed"
    UNVERIFIED = "unverified"
    CURRENCY_BLOCKED = "currency_blocked"


@dataclass(frozen=True, slots=True)
class CapacityEvidence:
    evidence_id: str
    merchant_id: str
    verified_spendable: Money
    protected_reserve: Money
    kind: EvidenceKind
    observed_at: datetime
    valid_until: datetime
    provenance: Provenance
    held: Money | None = None
    pending: Money | None = None
    included_receipt_ids: frozenset[str] = field(default_factory=frozenset)
    # Settled outgoing payments (commitment ids) already reflected in this balance (P5,
    # ADR-0012 §4.5). Only an observation that lists an outflow releases its commitment.
    included_outflow_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        check_id(self.evidence_id, "evidence_id")
        check_id(self.merchant_id, "merchant_id")
        if not isinstance(self.kind, EvidenceKind):
            raise CapacityError(f"invalid evidence kind {self.kind!r}")
        if self.provenance.source not in _EVIDENCE_SOURCES[self.kind]:
            raise CapacityError(
                f"{self.kind.value} evidence cannot come from {self.provenance.source.value}"
            )
        if not isinstance(self.included_receipt_ids, frozenset):
            raise CapacityError("included_receipt_ids must be a frozenset")
        for rid in self.included_receipt_ids:
            check_id(rid, "included receipt_id")
        if not isinstance(self.included_outflow_ids, frozenset):
            raise CapacityError("included_outflow_ids must be a frozenset")
        for oid in self.included_outflow_ids:
            check_id(oid, "included outflow id")
        check_aware(self.observed_at, "observed_at")
        check_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.observed_at:
            raise CapacityError("valid_until must be after observed_at")
        c = self.verified_spendable.currency
        for m in (self.protected_reserve, self.held, self.pending):
            if m is not None and m.currency != c:
                raise CurrencyMismatchError("all evidence amounts must share one currency")
        object.__setattr__(self, "held", self.held or Money.zero(c))
        object.__setattr__(self, "pending", self.pending or Money.zero(c))

    @property
    def currency(self) -> str:
        return self.verified_spendable.currency


@dataclass(frozen=True, slots=True)
class AvailabilityEvidence:
    """The separate proof that a received amount is usable (doc 05, D006)."""

    basis: AvailabilityBasis
    evidence_ref: str
    verified_at: datetime
    attested_by: str | None = None  # the attesting merchant, for MERCHANT_ATTESTED

    def __post_init__(self) -> None:
        if not isinstance(self.basis, AvailabilityBasis):
            raise CapacityError(f"invalid availability basis {self.basis!r}")
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
            raise CapacityError("availability evidence needs an evidence_ref")
        check_aware(self.verified_at, "verified_at")
        if (self.basis is AvailabilityBasis.MERCHANT_ATTESTED) != (self.attested_by is not None):
            raise CapacityError("attested_by is required exactly for merchant attestations")
        if self.attested_by is not None:
            check_id(self.attested_by, "attested_by")


@dataclass(frozen=True, slots=True)
class IncomingReceipt:
    receipt_id: str
    merchant_id: str
    amount: Money
    status: ReceiptStatus
    provenance: Provenance
    available_at: datetime | None = None
    availability: AvailabilityEvidence | None = None

    def __post_init__(self) -> None:
        check_id(self.receipt_id, "receipt_id")
        check_id(self.merchant_id, "merchant_id")
        if not isinstance(self.status, ReceiptStatus):
            raise CapacityError(f"invalid receipt status {self.status!r}")
        if self.available_at is not None:
            check_aware(self.available_at, "available_at")
        verified = self.status is ReceiptStatus.AVAILABLE_VERIFIED
        if verified and (self.available_at is None or self.availability is None):
            raise CapacityError(
                "a verified-available receipt needs available_at and availability evidence"
            )
        if not verified and self.availability is not None:
            raise CapacityError(f"a {self.status.value} receipt cannot carry availability")
        a = self.availability
        if a is None:
            return
        synthetic = self.provenance.is_synthetic
        if synthetic != (a.basis is AvailabilityBasis.SYNTHETIC):
            raise CapacityError("synthetic availability is only for synthetic receipts")
        if a.basis is AvailabilityBasis.MERCHANT_ATTESTED and a.attested_by != self.merchant_id:
            raise CapacityError("a merchant can attest only its own funds")
        if not synthetic:
            confirmation = {self.provenance.reference.strip()}
            if self.provenance.evidence_ref:
                confirmation |= {p.strip() for p in self.provenance.evidence_ref.split(";")}
            if a.evidence_ref.strip() in confirmation:
                raise CapacityError(
                    "availability evidence must be separate from the payment confirmation (D006)"
                )
        if a.verified_at < self.available_at:  # type: ignore[operator]
            raise CapacityError("availability cannot be verified before the funds are available")

    @property
    def usable_from(self) -> datetime | None:
        """When the receipt may first fund an outflow (available and verified)."""
        if self.availability is None or self.available_at is None:
            return None
        return max(self.available_at, self.availability.verified_at)


def mark_available(
    receipt: IncomingReceipt, availability: AvailabilityEvidence, available_at: datetime
) -> IncomingReceipt:
    """Explicit transition to AVAILABLE_VERIFIED. The result **replaces** the earlier
    record of the same receipt; supplying both to `compute_capacity` is a conflict."""
    if receipt.status is ReceiptStatus.AVAILABLE_VERIFIED:
        raise CapacityError(f"receipt {receipt.receipt_id} is already verified available")
    return replace(
        receipt,
        status=ReceiptStatus.AVAILABLE_VERIFIED,
        available_at=available_at,
        availability=availability,
    )


@dataclass(frozen=True, slots=True)
class Commitment:
    """Cash already reserved for an operation or plan leg."""

    commitment_id: str
    merchant_id: str
    plan_id: str
    amount: Money

    def __post_init__(self) -> None:
        check_id(self.commitment_id, "commitment_id")
        check_id(self.merchant_id, "merchant_id")
        check_id(self.plan_id, "plan_id")
        if self.amount.cents == 0:
            raise CapacityError("a commitment must be positive")


@dataclass(frozen=True, slots=True)
class ProjectedFunds:
    """Funds that may become usable later. Never counted as deployable."""

    receipt_id: str
    amount: Money
    status: ReceiptStatus
    expected_at: datetime | None
    reason: str


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    merchant_id: str
    at: datetime
    status: CapacityStatus
    evidence_id: str
    evidence_kind: EvidenceKind
    currency: str
    verified_spendable: Money
    verified_incoming: Money
    protected_reserve: Money
    committed: Money
    held: Money
    pending: Money
    raw_capacity_cents: int  # signed; negative means deficit
    deployable: Money  # always USD
    deficit: Money  # in evidence currency
    funds_available_from: datetime | None
    projected: tuple[ProjectedFunds, ...]
    reasons: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return self.status in (CapacityStatus.AVAILABLE, CapacityStatus.DEFICIT)


def _unique_commitments(commitments: Iterable[Commitment], merchant_id: str) -> list[Commitment]:
    seen: dict[str, Commitment] = {}
    for c in commitments:
        if c.merchant_id != merchant_id:
            continue
        if c.commitment_id in seen:
            raise DuplicateCommitmentError(f"commitment {c.commitment_id} counted twice")
        seen[c.commitment_id] = c
    return list(seen.values())


def _unique_receipts(
    receipts: Iterable[IncomingReceipt], merchant_id: str
) -> list[IncomingReceipt]:
    """One record per economic receipt of this merchant; identical copies collapse."""
    seen: dict[str, IncomingReceipt] = {}
    for r in receipts:
        if r.merchant_id != merchant_id:
            continue
        prior = seen.get(r.receipt_id)
        if prior is None:
            seen[r.receipt_id] = r
        elif prior != r:
            raise ReceiptConflictError(
                f"receipt {r.receipt_id} supplied twice with different content "
                f"({prior.status.value} {prior.amount} vs {r.status.value} {r.amount}); "
                "supply its current state once"
            )
    return list(seen.values())


def select_current_evidence(
    candidates: Iterable[CapacityEvidence], *, merchant_id: str, currency: str, at: datetime
) -> CapacityEvidence | None:
    """The authoritative observation of one merchant's balance in one currency at `at`:
    the latest one observed at or before `at`. Older observations are superseded."""
    check_aware(at, "at")
    pool: dict[str, CapacityEvidence] = {}
    for e in candidates:
        if e.merchant_id == merchant_id and e.currency == currency and e.observed_at <= at:
            if e.evidence_id in pool and pool[e.evidence_id] != e:
                raise EvidenceConflictError(f"evidence {e.evidence_id} has two versions")
            pool[e.evidence_id] = e
    if not pool:
        return None
    latest = max(e.observed_at for e in pool.values())
    top = [e for e in pool.values() if e.observed_at == latest]
    if len(top) > 1:
        ids = ", ".join(sorted(e.evidence_id for e in top))
        raise EvidenceConflictError(f"observations {ids} have the same observed_at")
    return top[0]


def check_not_superseded(
    evidence: CapacityEvidence, candidates: Iterable[CapacityEvidence], *, at: datetime
) -> None:
    """Raise if a newer observation of the same balance exists at `at`."""
    if evidence.observed_at > at:
        return  # not yet observed; compute_capacity reports NOT_YET_OBSERVED
    current = select_current_evidence(
        [evidence, *candidates], merchant_id=evidence.merchant_id, currency=evidence.currency, at=at
    )
    if current is not None and current.evidence_id != evidence.evidence_id:
        raise SupersededEvidenceError(
            f"evidence {evidence.evidence_id} is superseded by {current.evidence_id} "
            f"(observed {current.observed_at.isoformat()})"
        )


def compute_capacity(
    evidence: CapacityEvidence,
    commitments: Iterable[Commitment] = (),
    receipts: Iterable[IncomingReceipt] = (),
    *,
    at: datetime,
    allow_synthetic: bool = False,
) -> CapacitySnapshot:
    check_aware(at, "at")
    cur = evidence.currency
    mine = _unique_commitments(commitments, evidence.merchant_id)
    committed = Money.total((c.amount for c in mine if c.amount.currency == cur), cur)
    if any(c.amount.currency != cur for c in mine):
        raise CurrencyMismatchError("commitment currency differs from evidence currency")

    reasons: list[str] = []
    projected: list[ProjectedFunds] = []
    incoming = Money.zero(cur)
    latest_available: datetime | None = None
    synthetic_receipts = False
    for r in _unique_receipts(receipts, evidence.merchant_id):
        if r.receipt_id in evidence.included_receipt_ids:
            continue  # already reflected in the observed balance
        basis = r.availability.basis if r.availability else None
        synthetic_blocked = basis is AvailabilityBasis.SYNTHETIC and not allow_synthetic
        usable_from = r.usable_from
        usable = (
            r.status is ReceiptStatus.AVAILABLE_VERIFIED
            and r.amount.currency == cur
            and not synthetic_blocked
            and usable_from is not None
            and usable_from < at
        )
        if usable:
            incoming = incoming + r.amount
            latest_available = max(filter(None, (latest_available, usable_from)))
            synthetic_receipts |= basis is AvailabilityBasis.SYNTHETIC
        else:
            if r.amount.currency != cur:
                why = f"{r.amount.currency} receipt cannot fund {cur} capacity"
            elif r.status is not ReceiptStatus.AVAILABLE_VERIFIED:
                why = f"{r.status.value}: adds zero until separately verified available"
            elif synthetic_blocked:
                why = "synthetic availability counts only in an explicit simulation"
            else:
                why = "not available before this instant (no same-slot reuse)"
            projected.append(ProjectedFunds(r.receipt_id, r.amount, r.status, r.available_at, why))
    if synthetic_receipts:
        reasons.append("includes SYNTHETIC receipts: simulation only")

    raw = (
        evidence.verified_spendable.cents
        + incoming.cents
        - evidence.protected_reserve.cents
        - committed.cents
    )

    status: CapacityStatus
    if cur not in SPENDABLE_CURRENCIES:
        status = CapacityStatus.CURRENCY_BLOCKED
        reasons.append(f"{cur} balance is a recorded fact, not USD spending capacity (D008)")
    elif at < evidence.observed_at:
        status = CapacityStatus.NOT_YET_OBSERVED
        reasons.append("evidence is dated after the evaluation instant")
    elif at >= evidence.valid_until:
        status = CapacityStatus.STALE
        reasons.append("evidence expired; refresh before deploying cash")
    elif evidence.kind is EvidenceKind.UNKNOWN or (
        evidence.kind is EvidenceKind.SYNTHETIC and not allow_synthetic
    ):
        status = CapacityStatus.UNVERIFIED
        reasons.append(f"{evidence.kind.value} evidence cannot authorize spending")
    else:
        status = CapacityStatus.DEFICIT if raw < 0 else CapacityStatus.AVAILABLE
        if evidence.kind is EvidenceKind.SYNTHETIC:
            reasons.append("SYNTHETIC evidence: simulation only")

    usable = status in (CapacityStatus.AVAILABLE, CapacityStatus.DEFICIT)
    deployable = Money(max(0, raw), USD) if usable else Money.zero(USD)
    deficit = Money(max(0, -raw), cur) if usable else Money.zero(cur)
    available_from = None
    if usable and deployable.cents > 0:
        available_from = max(filter(None, (evidence.observed_at, latest_available)))

    return CapacitySnapshot(
        merchant_id=evidence.merchant_id,
        at=at,
        status=status,
        evidence_id=evidence.evidence_id,
        evidence_kind=evidence.kind,
        currency=cur,
        verified_spendable=evidence.verified_spendable,
        verified_incoming=incoming,
        protected_reserve=evidence.protected_reserve,
        committed=committed,
        held=evidence.held,  # type: ignore[arg-type]
        pending=evidence.pending,  # type: ignore[arg-type]
        raw_capacity_cents=raw,
        deployable=deployable,
        deficit=deficit,
        funds_available_from=available_from,
        projected=tuple(projected),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class PaymentAssessment:
    feasible: bool
    required: Money
    shortfall: Money
    explanation: str


def assess_outflow(
    snapshot: CapacitySnapshot, outflow: Money, fee: Money | None = None
) -> PaymentAssessment:
    """Can this merchant pay `outflow` (+ `fee`) now while keeping its reserve?

    Liquidity gap (doc 04): max(0, outflow + fee + reserve + committed - spendable -
    verified incoming) = max(0, outflow + fee - raw_capacity). Includes any deficit.
    """
    fee = fee or Money.zero(outflow.currency)
    required = outflow + fee
    if not snapshot.usable:
        return PaymentAssessment(
            False, required, required, f"{snapshot.status.value}: " + "; ".join(snapshot.reasons)
        )
    if required.currency != USD:
        raise CurrencyMismatchError("outflows are planned in USD only (D008)")
    gap = max(0, required.cents - snapshot.raw_capacity_cents)
    shortfall = Money(gap, USD)
    available = snapshot.verified_spendable + snapshot.verified_incoming
    expl = (
        f"needs {required} + {snapshot.protected_reserve} reserve"
        + (f" + {snapshot.committed} committed" if snapshot.committed.cents else "")
        + f", has {available} verified available"
    )
    if gap:
        return PaymentAssessment(False, required, shortfall, f"{expl}; short by {shortfall}")
    return PaymentAssessment(True, required, shortfall, f"{expl}; feasible")


def commit_capacity(
    evidence: CapacityEvidence,
    existing: Iterable[Commitment],
    new: Commitment,
    *,
    at: datetime,
    receipts: Iterable[IncomingReceipt] = (),
    allow_synthetic: bool = False,
) -> tuple[Commitment, ...]:
    """Pure check-and-reserve. Returns the new commitment list or raises."""
    existing = tuple(existing)
    if new.merchant_id != evidence.merchant_id:
        raise CapacityError("commitment is for a different merchant")
    if any(c.commitment_id == new.commitment_id for c in existing):
        raise DuplicateCommitmentError(f"commitment {new.commitment_id} already reserved")
    snap = compute_capacity(evidence, existing, receipts, at=at, allow_synthetic=allow_synthetic)
    if not snap.usable:
        raise InsufficientCapacityError(f"{snap.status.value}: " + "; ".join(snap.reasons))
    if new.amount.currency != USD or new.amount.cents > snap.deployable.cents:
        raise InsufficientCapacityError(
            f"requested {new.amount}, deployable {snap.deployable} "
            f"(spendable {snap.verified_spendable}, reserve {snap.protected_reserve}, "
            f"committed {snap.committed})"
        )
    return (*existing, new)
