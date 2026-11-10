"""Liquidity capacity engine (pure, deterministic; no database or provider access).

    raw_capacity = verified_spendable + verified_incoming - protected_reserve - committed
    deployable   = max(0, raw_capacity)        deficit = max(0, -raw_capacity)

Rules (doc 04/05, D006, D008):
- Only USD evidence can produce deployable capacity. Other currencies are reported as
  CURRENCY_BLOCKED with zero deployable USD.
- Evidence counts only inside its validity window [observed_at, valid_until). Stale,
  future-dated, unknown-kind and (unless simulating) synthetic evidence deploy zero.
- Held, pending, promised and provider-confirmed (e.g. PayPal `PAID`) receipts add zero.
  A receipt adds capacity only if it is AVAILABLE_VERIFIED, available strictly before the
  instant being evaluated (no same-slot reuse), and not already included in the evidence.
- Every active commitment of the merchant is subtracted once; a duplicate commitment id
  is an error, so two plans cannot count the same cash twice.
- A negative raw capacity is reported as a deficit, never hidden by the max().
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from api.domain.invoice import Provenance, check_aware, check_id
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


class EvidenceKind(StrEnum):
    PROVIDER_VERIFIED = "provider_verified"
    MERCHANT_ATTESTED = "merchant_attested"
    SYNTHETIC = "synthetic"  # usable only in explicitly labeled simulations
    UNKNOWN = "unknown"


class ReceiptStatus(StrEnum):
    PROMISED = "promised"  # invoice or promise, nothing received
    PROVIDER_CONFIRMED = "provider_confirmed"  # e.g. PayPal invoice PAID (D006: not usable)
    HELD = "held"
    AVAILABLE_VERIFIED = "available_verified"  # separately verified as usable


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

    def __post_init__(self) -> None:
        check_id(self.evidence_id, "evidence_id")
        check_id(self.merchant_id, "merchant_id")
        if not isinstance(self.kind, EvidenceKind):
            raise CapacityError(f"invalid evidence kind {self.kind!r}")
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
class IncomingReceipt:
    receipt_id: str
    merchant_id: str
    amount: Money
    status: ReceiptStatus
    provenance: Provenance
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        check_id(self.receipt_id, "receipt_id")
        check_id(self.merchant_id, "merchant_id")
        if not isinstance(self.status, ReceiptStatus):
            raise CapacityError(f"invalid receipt status {self.status!r}")
        if self.available_at is not None:
            check_aware(self.available_at, "available_at")
        if self.status is ReceiptStatus.AVAILABLE_VERIFIED and self.available_at is None:
            raise CapacityError("a verified-available receipt needs available_at")


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
    for r in receipts:
        if r.merchant_id != evidence.merchant_id:
            continue
        if r.receipt_id in evidence.included_receipt_ids:
            continue  # already reflected in the observed balance
        usable = (
            r.status is ReceiptStatus.AVAILABLE_VERIFIED
            and r.amount.currency == cur
            and r.available_at is not None
            and r.available_at < at
        )
        if usable:
            incoming = incoming + r.amount
            latest_available = max(filter(None, (latest_available, r.available_at)))
        else:
            if r.amount.currency != cur:
                why = f"{r.amount.currency} receipt cannot fund {cur} capacity"
            elif r.status is not ReceiptStatus.AVAILABLE_VERIFIED:
                why = f"{r.status.value}: adds zero until separately verified available"
            else:
                why = "not available before this instant (no same-slot reuse)"
            projected.append(ProjectedFunds(r.receipt_id, r.amount, r.status, r.available_at, why))

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
