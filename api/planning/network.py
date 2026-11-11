"""Permissioned payment network for the P2 planner (SYNTHETIC simulation scope).

A directed multigraph: merchants are nodes, immutable invoice obligations are edges
(debtor -> creditor), and each edge has one or more candidate *payment options* (legs)
that would discharge it under specific terms:

- IN_FORCE: the authorized terms in force at `as_of` (original or an accepted amendment).
- OPEN_OFFER: a DRAFT revision already signed by the creditor; executing it means the
  debtor accepts the offer before its deadline and pays in the same slot.
- NEGOTIATED_PROPOSAL: a DRAFT not signed by both parties (e.g. an AI-suggested
  counteroffer). Usable only when a run explicitly enables negotiated changes (B5), and
  then only as an *assumption* that both merchants approve. Elide never amends an invoice.

Time is a discrete `SlotClock`. A payment in slot t debits the payer at slot t; the payee
can use the proceeds from slot `t + lag` with lag >= 1 (`AvailabilityModel`), so a receipt
can never fund a payment in the slot it arrives (no same-slot reuse).

Cash is kept in separate categories per merchant (`MerchantPosition`): verified available
at slot 0, protected reserve, existing commitments, verified future inflows, and
held / pending / unverified / currency-blocked amounts that never count.

This module is data plus structural validation (identities, references, exact invoice
discharge). Economic and consent rules (disputes, refusals, unknown fees, participation,
currency, merchant policies) are enforced by the solver, which skips such legs, and
independently by the verifier, which rejects them. Nothing here is provider evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from api.domain.invoice import DisputeStatus, Provenance, check_aware, check_id
from api.domain.money import USD, Money

NETWORK_MODEL_VERSION = "elide-network-v1"
ZERO_USD = Money(0, USD)


class NetworkError(ValueError):
    pass


class DuplicateObligationError(NetworkError):
    pass


class DuplicateLegError(NetworkError):
    pass


class InvalidLegError(NetworkError):
    pass


class SameSlotReuseError(NetworkError):
    """An availability lag below one slot would let a receipt fund a same-slot payment."""


# --- time ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SlotClock:
    """Slot t covers [starts[t], starts[t+1]); a payment in slot t executes at starts[t]."""

    starts: tuple[datetime, ...]

    def __post_init__(self) -> None:
        if not self.starts:
            raise NetworkError("a clock needs at least one slot")
        for s in self.starts:
            try:
                check_aware(s, "slot start")
            except ValueError as exc:
                raise NetworkError(str(exc)) from exc
        if any(b <= a for a, b in zip(self.starts, self.starts[1:], strict=False)):
            raise NetworkError("slot starts must be strictly increasing")

    @classmethod
    def uniform(cls, start: datetime, step: timedelta, count: int) -> SlotClock:
        if count < 1 or step <= timedelta(0):
            raise NetworkError("need count >= 1 and a positive step")
        return cls(tuple(start + step * i for i in range(count)))

    def __len__(self) -> int:
        return len(self.starts)

    def start(self, t: int) -> datetime:
        return self.starts[t]

    def last_slot_at_or_before(self, instant: datetime) -> int | None:
        """The last slot that starts at or before `instant` (e.g. the due slot)."""
        found = None
        for t, s in enumerate(self.starts):
            if s <= instant:
                found = t
        return found

    def last_slot_before(self, instant: datetime) -> int | None:
        """The last slot that starts strictly before `instant` (e.g. an offer deadline)."""
        found = None
        for t, s in enumerate(self.starts):
            if s < instant:
                found = t
        return found

    def first_slot_after(self, instant: datetime) -> int | None:
        """The first slot that starts strictly after `instant` (funds usable from then)."""
        return next((t for t, s in enumerate(self.starts) if s > instant), None)


@dataclass(frozen=True, slots=True)
class AvailabilityModel:
    """Slots between a payment and the payee being able to spend its proceeds.

    `synthetic_zero_lag` (lag 1): received in slot t, usable from slot t+1 - zero real
    delay between sequential slots, but never within the same slot.
    """

    name: str
    default_lag: int = 1
    lags: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        for lag in (self.default_lag, *(lag for _, lag in self.lags)):
            if isinstance(lag, bool) or not isinstance(lag, int) or lag < 1:
                raise SameSlotReuseError(
                    f"availability lag must be an int >= 1 slot, got {lag!r}: a receipt "
                    "cannot fund a payment in the slot it arrives"
                )

    def lag(self, merchant_id: str) -> int:
        return dict(self.lags).get(merchant_id, self.default_lag)


SYNTHETIC_ZERO_LAG = AvailabilityModel("synthetic_zero_lag", 1)


# --- consent and fees ------------------------------------------------------------------
class ConsentBasis(StrEnum):
    SYNTHETIC_ASSUMPTION = "synthetic_assumption"  # simulation assumes this approval
    RECORDED_DECISION = "recorded_decision"  # actual approval evidence (P4); none in P2
    REFUSED = "refused"  # the merchant declined; the leg is not executable


AUTH_ACTIONS = frozenset({"authorize_payment", "accept_offer", "sign_terms"})


@dataclass(frozen=True, slots=True)
class Authorization:
    merchant_id: str
    action: str
    basis: ConsentBasis
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        check_id(self.merchant_id, "merchant_id")
        if self.action not in AUTH_ACTIONS:
            raise NetworkError(f"unknown authorization action {self.action!r}")
        if not isinstance(self.basis, ConsentBasis):
            raise NetworkError(f"invalid consent basis {self.basis!r}")
        if (self.basis is ConsentBasis.RECORDED_DECISION) != (self.evidence_ref is not None):
            raise NetworkError(
                "a recorded decision needs an evidence reference; assumptions and "
                "refusals carry none"
            )


class FeeStatus(StrEnum):
    KNOWN = "known"  # exact amounts
    CONDITIONAL = "conditional"  # amounts are worst-case ceilings
    UNKNOWN = "unknown"  # no bound: the leg cannot be planned


@dataclass(frozen=True, slots=True)
class FeeTerms:
    """`payer_fee` is added to the payer's total debit; `payee_fee` is deducted from the
    payee's proceeds. `source` says where the number comes from (never a fee model)."""

    payer_fee: Money = ZERO_USD
    payee_fee: Money = ZERO_USD
    status: FeeStatus = FeeStatus.KNOWN
    source: str = "synthetic"

    def __post_init__(self) -> None:
        if not isinstance(self.status, FeeStatus):
            raise NetworkError(f"invalid fee status {self.status!r}")
        for m in (self.payer_fee, self.payee_fee):
            if not isinstance(m, Money) or m.currency != USD:
                raise NetworkError("fees are USD Money (D008)")
        if self.status is FeeStatus.UNKNOWN and (self.payer_fee.cents or self.payee_fee.cents):
            raise NetworkError("an unknown fee has no amount; use CONDITIONAL with a ceiling")


NO_FEES = FeeTerms()


# --- edges and legs ------------------------------------------------------------------
class TermsKind(StrEnum):
    IN_FORCE = "in_force"
    OPEN_OFFER = "open_offer"
    NEGOTIATED_PROPOSAL = "negotiated_proposal"


@dataclass(frozen=True, slots=True)
class ObligationEdge:
    obligation_id: str
    debtor: str
    creditor: str
    face: Money
    cash_applied: Money
    in_force_concession: Money
    revision_no: int
    due_at: datetime
    provenance: Provenance
    dispute: DisputeStatus = DisputeStatus.NONE
    provider_ref: str | None = None

    def __post_init__(self) -> None:
        check_id(self.obligation_id, "obligation_id")
        check_id(self.debtor, "debtor")
        check_id(self.creditor, "creditor")
        if self.debtor == self.creditor:
            raise NetworkError("an obligation needs two different merchants")
        check_aware(self.due_at, "due_at")
        if not isinstance(self.dispute, DisputeStatus):
            raise NetworkError(f"invalid dispute status {self.dispute!r}")
        _ = self.open  # raises if cash + concession exceed face or currencies differ

    @property
    def currency(self) -> str:
        return self.face.currency

    @property
    def open(self) -> Money:
        return self.face - self.cash_applied - self.in_force_concession


@dataclass(frozen=True, slots=True)
class PaymentOption:
    option_id: str
    obligation_id: str
    revision_no: int
    terms: TermsKind
    payer: str
    payee: str
    cash: Money  # paid to the payee; with `concession` it discharges the invoice exactly
    concession: Money  # total concession under these terms
    fee: FeeTerms
    authorizations: tuple[Authorization, ...]
    provenance: Provenance
    expires_at: datetime | None = None  # offer deadline: accept-and-pay strictly before

    def __post_init__(self) -> None:
        check_id(self.option_id, "option_id")
        check_id(self.obligation_id, "obligation_id")
        if not isinstance(self.terms, TermsKind):
            raise NetworkError(f"invalid terms kind {self.terms!r}")
        if self.cash.cents <= 0:
            raise InvalidLegError(f"{self.option_id}: a leg must move a positive amount")
        if self.cash.currency != self.concession.currency:
            raise InvalidLegError(f"{self.option_id}: cash and concession currencies differ")
        if self.fee.payee_fee.cents > self.cash.cents:
            raise InvalidLegError(f"{self.option_id}: payee fee exceeds the payment")
        if self.expires_at is not None:  # None: open for the whole horizon
            check_aware(self.expires_at, "expires_at")


# --- merchants ------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MerchantPolicy:
    """Private, merchant-confirmed rules (synthetic in P2). Never shown to others."""

    merchant_id: str
    participating: bool = True
    hurdle_bps: int = 0  # annual opportunity cost of cash paid before the due slot
    min_early_discount: Money = ZERO_USD  # extra concession needed to pay early
    min_net_benefit_cents: int = 0  # discount - time cost needed to pay early
    early_receipt_value_bps: int = 0  # stated annual value of receiving cash early
    max_fee_per_leg: Money | None = None
    max_concession_given: Money | None = None  # as payee, on top of in-force terms
    # As payee, for a leg that changes its outcome (extra concession or early payment):
    # stated early-receipt value - extra concession - payee fee - receipt_cost_cents must
    # be >= this. None = no declared threshold (the merchant may still have signed terms
    # that are a modeled loss; plans flag that, they never call it a win-win).
    min_receipt_net_benefit_cents: int | None = None
    receipt_cost_cents: int = 0  # declared modeled cost of accepting changed terms
    prohibited_counterparties: frozenset[str] = frozenset()
    source: str = "synthetic"

    def __post_init__(self) -> None:
        check_id(self.merchant_id, "merchant_id")
        ints = (
            self.hurdle_bps,
            self.early_receipt_value_bps,
            self.min_net_benefit_cents,
            self.receipt_cost_cents,
            0 if self.min_receipt_net_benefit_cents is None else self.min_receipt_net_benefit_cents,
        )
        for v in ints:
            if isinstance(v, bool) or not isinstance(v, int):
                raise NetworkError("policy rates and benefits are ints")
        if self.hurdle_bps < 0 or self.early_receipt_value_bps < 0 or self.receipt_cost_cents < 0:
            raise NetworkError("rates and costs cannot be negative")


@dataclass(frozen=True, slots=True)
class ScheduledInflow:
    """Already-known funds that become verified available at `available_slot`."""

    receipt_id: str
    amount: Money
    available_slot: int
    basis: str


@dataclass(frozen=True, slots=True)
class UnverifiedFunds:
    """Held, pending, provider-confirmed or otherwise unverified funds: count for zero."""

    receipt_id: str
    amount: Money
    reason: str


@dataclass(frozen=True, slots=True)
class MerchantPosition:
    merchant_id: str
    available: Money  # verified spendable USD usable from slot 0
    protected_reserve: Money
    committed: Money
    evidence_id: str
    evidence_kind: str
    valid_until: datetime | None  # opening evidence cannot fund slots starting after it
    held: Money = ZERO_USD
    pending: Money = ZERO_USD
    inflows: tuple[ScheduledInflow, ...] = ()
    unverified: tuple[UnverifiedFunds, ...] = ()
    blocked: tuple[Money, ...] = ()  # non-USD balances: recorded facts, never capacity
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_id(self.merchant_id, "merchant_id")
        for m in (self.available, self.protected_reserve, self.committed, self.held, self.pending):
            if m.currency != USD:
                raise NetworkError("positions are planned in USD only (D008)")
        if any(b.currency == USD for b in self.blocked):
            raise NetworkError("blocked balances are non-USD by definition")
        if self.valid_until is not None:
            check_aware(self.valid_until, "valid_until")

    @property
    def base_slack_cents(self) -> int:
        """available - reserve - committed (negative = already in deficit)."""
        return self.available.cents - self.protected_reserve.cents - self.committed.cents


@dataclass(frozen=True, slots=True)
class Exclusion:
    subject_id: str
    obligation_id: str
    code: str
    explanation: str


# --- network --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Network:
    network_id: str
    as_of: datetime
    clock: SlotClock
    availability: AvailabilityModel
    focal_merchant: str
    positions: tuple[MerchantPosition, ...]
    policies: tuple[MerchantPolicy, ...]
    obligations: tuple[ObligationEdge, ...]
    options: tuple[PaymentOption, ...]
    exclusions: tuple[Exclusion, ...] = ()
    labels: tuple[str, ...] = ("SYNTHETIC_NETWORK",)
    _index: dict[str, Any] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        check_id(self.network_id, "network_id")
        check_aware(self.as_of, "as_of")
        if self.clock.start(0) != self.as_of:
            raise NetworkError("slot 0 must start at the planning instant `as_of`")
        merchants = [p.merchant_id for p in self.positions]
        if len(set(merchants)) != len(merchants):
            raise NetworkError("one position per merchant")
        if sorted(p.merchant_id for p in self.policies) != sorted(merchants):
            raise NetworkError("exactly one policy per merchant")
        if self.focal_merchant not in merchants:
            raise NetworkError("focal merchant has no position")
        for p in self.positions:
            for i in p.inflows:
                if not 0 <= i.available_slot < len(self.clock):
                    raise NetworkError(f"inflow {i.receipt_id} outside the horizon")
        edges: dict[str, ObligationEdge] = {}
        refs: set[str] = set()
        for e in self.obligations:
            if e.obligation_id in edges:
                raise DuplicateObligationError(f"obligation {e.obligation_id} appears twice")
            if e.provider_ref is not None:
                if e.provider_ref in refs:
                    raise DuplicateObligationError(
                        f"provider invoice {e.provider_ref} backs two obligations"
                    )
                refs.add(e.provider_ref)
            for m in (e.debtor, e.creditor):
                if m not in merchants:
                    raise NetworkError(f"{e.obligation_id}: merchant {m} has no position")
            edges[e.obligation_id] = e
        opts: dict[str, PaymentOption] = {}
        for o in self.options:
            if o.option_id in opts:
                raise DuplicateLegError(f"leg {o.option_id} appears twice")
            e = edges.get(o.obligation_id)
            if e is None:
                raise InvalidLegError(f"{o.option_id}: unknown obligation {o.obligation_id}")
            if (o.payer, o.payee) != (e.debtor, e.creditor):
                raise InvalidLegError(f"{o.option_id}: payer/payee must be debtor/creditor")
            if o.cash.currency != e.currency:
                raise InvalidLegError(f"{o.option_id}: currency differs from the invoice")
            if o.cash + o.concession + e.cash_applied != e.face:
                raise InvalidLegError(
                    f"{o.option_id}: cash {o.cash} + concession {o.concession} + applied "
                    f"{e.cash_applied} must equal face {e.face}"
                )
            if o.terms is TermsKind.IN_FORCE and (
                o.revision_no != e.revision_no or o.concession != e.in_force_concession
            ):
                raise InvalidLegError(f"{o.option_id}: in-force leg must use in-force terms")
            opts[o.option_id] = o
        object.__setattr__(
            self,
            "_index",
            {
                "positions": {p.merchant_id: p for p in self.positions},
                "policies": {p.merchant_id: p for p in self.policies},
                "edges": edges,
                "options": opts,
            },
        )

    # -- lookups --------------------------------------------------------------------------
    @property
    def merchants(self) -> tuple[str, ...]:
        return tuple(sorted(p.merchant_id for p in self.positions))

    def position(self, merchant_id: str) -> MerchantPosition:
        return self._index["positions"][merchant_id]

    def policy(self, merchant_id: str) -> MerchantPolicy:
        return self._index["policies"][merchant_id]

    def obligation(self, obligation_id: str) -> ObligationEdge:
        return self._index["edges"][obligation_id]

    def option(self, option_id: str) -> PaymentOption:
        return self._index["options"][option_id]

    def has_option(self, option_id: str) -> bool:
        return option_id in self._index["options"]

    def options_for(self, obligation_id: str) -> tuple[PaymentOption, ...]:
        return tuple(
            sorted(
                (o for o in self.options if o.obligation_id == obligation_id),
                key=lambda o: o.option_id,
            )
        )

    # -- canonical form --------------------------------------------------------------------
    def to_canonical(self) -> dict[str, Any]:
        return {
            "model_version": NETWORK_MODEL_VERSION,
            "network_id": self.network_id,
            "as_of": self.as_of.isoformat(),
            "slots": [s.isoformat() for s in self.clock.starts],
            "availability": {
                "name": self.availability.name,
                "default_lag": self.availability.default_lag,
                "lags": sorted(self.availability.lags),
            },
            "focal_merchant": self.focal_merchant,
            "positions": [_jsonable(p) for p in sorted(self.positions, key=_mid)],
            "policies": [_jsonable(p) for p in sorted(self.policies, key=_mid)],
            "obligations": [
                _jsonable(e) for e in sorted(self.obligations, key=lambda e: e.obligation_id)
            ],
            "options": [_jsonable(o) for o in sorted(self.options, key=lambda o: o.option_id)],
            "exclusions": [_jsonable(x) for x in self.exclusions],
            "labels": list(self.labels),
        }

    def digest(self) -> str:
        blob = json.dumps(self.to_canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def _mid(p: MerchantPosition | MerchantPolicy) -> str:
    return p.merchant_id


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Money):
        return {"cents": obj.cents, "currency": obj.currency}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, frozenset):
        return sorted(_jsonable(x) for x in obj)
    if isinstance(obj, tuple | list):
        return [_jsonable(x) for x in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {
            k: _jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__ if not k.startswith("_")
        }
    return obj
