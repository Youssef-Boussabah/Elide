"""Invoice obligations, immutable revisions and settlement events.

Accounting identity enforced after every change (doc 04, without set-off):

    original_open = cash_applied + authorized_concession + remaining_open

- `Obligation` is the original invoice. Its identity and face amount never change.
- `ObligationRevision`s are append-only and numbered 1, 2, 3, ... Revision 1 states the
  original terms (no concession). A concession counts only in an AUTHORIZED revision that
  both debtor and creditor authorized. DRAFT revisions (e.g. an AI-proposed discount) and
  REJECTED revisions never count.
- `SettlementEvent`s record cash applied and reversals of it. A reversal references the
  event it reverses; history is never edited or deleted.
- Legal noncash set-off is out of scope (D011) and has no representation here.

`InvoiceRecord` is immutable: every operation returns a new record or raises, so a
rejected change can never leave a half-applied state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api.domain.money import CurrencyMismatchError, Money

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")


class InvoiceError(ValueError):
    pass


class InvalidInvoiceError(InvoiceError):
    pass


class DuplicateObligationError(InvoiceError):
    pass


class ConflictingRevisionError(InvoiceError):
    pass


class UnauthorizedConcessionError(InvoiceError):
    pass


class ExpiredRevisionError(InvoiceError):
    pass


class OverSettlementError(InvoiceError):
    """Cash + concession would exceed the original face amount."""


class DuplicateEventError(InvoiceError):
    pass


class InvalidReversalError(InvoiceError):
    pass


class OutOfOrderError(InvoiceError):
    """A change is dated before something already recorded (no backdating)."""


def check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidInvoiceError(f"invalid {what}: {value!r}")
    return value


def check_aware(value: object, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInvoiceError(f"{what} must be a timezone-aware datetime")
    return value


def check_timezone(name: object) -> str:
    if not isinstance(name, str):
        raise InvalidInvoiceError(f"invalid timezone {name!r}")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidInvoiceError(f"unknown timezone {name!r}") from exc
    return name


class SourceKind(StrEnum):
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    MERCHANT_ENTERED = "merchant_entered"
    MERCHANT_ATTESTED = "merchant_attested"
    PAYPAL_SANDBOX = "paypal_sandbox"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a fact came from. `evidence_ref` points at a redacted evidence file or run."""

    source: SourceKind
    reference: str
    recorded_at: datetime
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceKind):
            raise InvalidInvoiceError(f"invalid source kind {self.source!r}")
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise InvalidInvoiceError("provenance reference is required")
        check_aware(self.recorded_at, "provenance.recorded_at")

    @property
    def is_synthetic(self) -> bool:
        return self.source is SourceKind.SYNTHETIC_FIXTURE


@dataclass(frozen=True, slots=True)
class Merchant:
    merchant_id: str
    display_name: str
    business_tz: str

    def __post_init__(self) -> None:
        check_id(self.merchant_id, "merchant_id")
        check_timezone(self.business_tz)


@dataclass(frozen=True, slots=True)
class ProviderRef:
    """External identity of an invoice, e.g. ("paypal", "sandbox", "INV2-...")."""

    provider: str
    environment: str
    resource_id: str

    def __post_init__(self) -> None:
        for v, what in ((self.provider, "provider"), (self.environment, "environment")):
            check_id(v, what)
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise InvalidInvoiceError("provider resource_id is required")


@dataclass(frozen=True, slots=True)
class Obligation:
    obligation_id: str
    debtor: str
    creditor: str
    face: Money
    issued_at: datetime
    provenance: Provenance
    provider_ref: ProviderRef | None = None

    def __post_init__(self) -> None:
        check_id(self.obligation_id, "obligation_id")
        check_id(self.debtor, "debtor")
        check_id(self.creditor, "creditor")
        if self.debtor == self.creditor:
            raise InvalidInvoiceError("debtor and creditor must differ")
        if not isinstance(self.face, Money) or self.face.cents == 0:
            raise InvalidInvoiceError("face must be a positive Money amount")
        check_aware(self.issued_at, "issued_at")

    @property
    def currency(self) -> str:
        return self.face.currency


class RevisionStatus(StrEnum):
    DRAFT = "draft"  # proposed terms; never counts
    AUTHORIZED = "authorized"  # in force once both parties authorized
    REJECTED = "rejected"


class DisputeStatus(StrEnum):
    NONE = "none"
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class ObligationRevision:
    obligation_id: str
    revision_no: int
    status: RevisionStatus
    due_at: datetime
    business_tz: str
    concession: Money
    created_at: datetime
    provenance: Provenance
    dispute: DisputeStatus = DisputeStatus.NONE
    expires_at: datetime | None = None
    authorized_by: frozenset[str] = field(default_factory=frozenset)
    supersedes: int | None = None

    def __post_init__(self) -> None:
        check_id(self.obligation_id, "obligation_id")
        if isinstance(self.revision_no, bool) or not isinstance(self.revision_no, int):
            raise InvalidInvoiceError("revision_no must be an int")
        if self.revision_no < 1:
            raise InvalidInvoiceError("revision_no starts at 1")
        if not isinstance(self.status, RevisionStatus):
            raise InvalidInvoiceError(f"invalid status {self.status!r}")
        if not isinstance(self.dispute, DisputeStatus):
            raise InvalidInvoiceError(f"invalid dispute status {self.dispute!r}")
        check_aware(self.due_at, "due_at")
        check_aware(self.created_at, "created_at")
        check_timezone(self.business_tz)
        if not isinstance(self.concession, Money):
            raise InvalidInvoiceError("concession must be Money")
        if self.expires_at is not None:
            check_aware(self.expires_at, "expires_at")
        if not isinstance(self.authorized_by, frozenset):
            raise InvalidInvoiceError("authorized_by must be a frozenset")

    def is_expired(self, at: datetime) -> bool:
        return self.expires_at is not None and at >= self.expires_at


class SettlementKind(StrEnum):
    CASH_APPLIED = "cash_applied"
    CASH_REVERSED = "cash_reversed"


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    event_id: str
    obligation_id: str
    kind: SettlementKind
    amount: Money
    occurred_at: datetime
    provenance: Provenance
    reverses_event_id: str | None = None

    def __post_init__(self) -> None:
        check_id(self.event_id, "event_id")
        check_id(self.obligation_id, "obligation_id")
        if not isinstance(self.kind, SettlementKind):
            raise InvalidInvoiceError(f"invalid settlement kind {self.kind!r}")
        if not isinstance(self.amount, Money) or self.amount.cents == 0:
            raise InvalidInvoiceError("settlement amount must be a positive Money amount")
        check_aware(self.occurred_at, "occurred_at")
        if self.kind is SettlementKind.CASH_REVERSED and not self.reverses_event_id:
            raise InvalidReversalError("a reversal must reference the event it reverses")
        if self.kind is SettlementKind.CASH_APPLIED and self.reverses_event_id is not None:
            raise InvalidReversalError("cash applied cannot reference a reversed event")


class SettlementStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_SETTLED = "partially_settled"
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class SettlementPosition:
    original_open: Money
    cash_applied: Money
    authorized_concession: Money
    remaining_open: Money
    status: SettlementStatus
    dispute: DisputeStatus
    revision_no: int

    def __post_init__(self) -> None:
        # The identity is checked on every position ever produced.
        total = self.cash_applied + self.authorized_concession + self.remaining_open
        if total != self.original_open:
            raise AssertionError(f"settlement identity broken: {total} != {self.original_open}")


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    obligation: Obligation
    revisions: tuple[ObligationRevision, ...]
    events: tuple[SettlementEvent, ...] = ()

    # -- construction -------------------------------------------------------------------
    @classmethod
    def open(cls, obligation: Obligation, original_terms: ObligationRevision) -> InvoiceRecord:
        r = original_terms
        if r.obligation_id != obligation.obligation_id or r.revision_no != 1:
            raise InvalidInvoiceError("original terms must be revision 1 of this obligation")
        if r.status is not RevisionStatus.AUTHORIZED or r.concession.cents != 0:
            raise InvalidInvoiceError("revision 1 must be the authorized original terms")
        if r.concession.currency != obligation.currency:
            raise CurrencyMismatchError("concession currency differs from invoice currency")
        if r.created_at < obligation.issued_at:
            raise OutOfOrderError("original terms predate the invoice")
        return cls(obligation, (r,))

    # -- helpers ------------------------------------------------------------------------
    @property
    def currency(self) -> str:
        return self.obligation.currency

    @property
    def last_recorded_at(self) -> datetime:
        times = [r.created_at for r in self.revisions] + [e.occurred_at for e in self.events]
        return max(times)

    def _check_time(self, at: datetime) -> None:
        if at < self.last_recorded_at:
            raise OutOfOrderError(f"{at.isoformat()} is before {self.last_recorded_at.isoformat()}")

    def in_force_revision(self, at: datetime | None = None) -> ObligationRevision:
        candidates = [
            r
            for r in self.revisions
            if r.status is RevisionStatus.AUTHORIZED and (at is None or r.created_at <= at)
        ]
        if not candidates:
            raise InvalidInvoiceError("no authorized revision in force at that time")
        return candidates[-1]

    def _cash_applied_cents(self, events: tuple[SettlementEvent, ...]) -> int:
        applied = sum(e.amount.cents for e in events if e.kind is SettlementKind.CASH_APPLIED)
        reversed_ = sum(e.amount.cents for e in events if e.kind is SettlementKind.CASH_REVERSED)
        return applied - reversed_

    # -- revisions ----------------------------------------------------------------------
    def add_revision(self, rev: ObligationRevision) -> InvoiceRecord:
        if rev.obligation_id != self.obligation.obligation_id:
            raise InvalidInvoiceError("revision belongs to a different obligation")
        existing = next((r for r in self.revisions if r.revision_no == rev.revision_no), None)
        if existing is not None:
            if existing == rev:
                return self  # identical replay: idempotent
            raise ConflictingRevisionError(f"revision {rev.revision_no} already exists")
        if rev.revision_no != self.revisions[-1].revision_no + 1:
            raise ConflictingRevisionError(
                f"expected revision {self.revisions[-1].revision_no + 1}, got {rev.revision_no}"
            )
        if rev.concession.currency != self.currency:
            raise CurrencyMismatchError("concession currency differs from invoice currency")
        if rev.concession > self.obligation.face:
            raise OverSettlementError("concession exceeds the original face amount")
        self._check_time(rev.created_at)

        if rev.supersedes is not None:
            prior = next((r for r in self.revisions if r.revision_no == rev.supersedes), None)
            if prior is None:
                raise ConflictingRevisionError(f"superseded revision {rev.supersedes} not found")
            if prior.status is RevisionStatus.DRAFT and prior.is_expired(rev.created_at):
                raise ExpiredRevisionError(f"draft revision {prior.revision_no} has expired")

        if rev.status is RevisionStatus.AUTHORIZED:
            parties = {self.obligation.debtor, self.obligation.creditor}
            if not parties <= rev.authorized_by:
                raise UnauthorizedConcessionError(
                    "authorized terms need both debtor and creditor authorization"
                )
            cash = self._cash_applied_cents(self.events)
            if cash + rev.concession.cents > self.obligation.face.cents:
                raise OverSettlementError("cash applied plus new concession exceeds face")
        elif rev.authorized_by - {self.obligation.debtor, self.obligation.creditor}:
            raise UnauthorizedConcessionError("only the two invoice parties can authorize terms")
        return replace(self, revisions=(*self.revisions, rev))

    # -- settlement events --------------------------------------------------------------
    def apply(self, event: SettlementEvent) -> InvoiceRecord:
        if event.obligation_id != self.obligation.obligation_id:
            raise InvalidInvoiceError("event belongs to a different obligation")
        existing = next((e for e in self.events if e.event_id == event.event_id), None)
        if existing is not None:
            if existing == event:
                return self  # identical replay: counted once
            raise DuplicateEventError(f"event {event.event_id} already recorded differently")
        if event.amount.currency != self.currency:
            raise CurrencyMismatchError(
                f"event in {event.amount.currency}, invoice in {self.currency}"
            )
        self._check_time(event.occurred_at)

        if event.kind is SettlementKind.CASH_APPLIED:
            concession = self.in_force_revision(event.occurred_at).concession.cents
            cash = self._cash_applied_cents(self.events)
            if cash + event.amount.cents + concession > self.obligation.face.cents:
                raise OverSettlementError(
                    "payment exceeds remaining open amount; record overpayment separately"
                )
        else:
            target = next((e for e in self.events if e.event_id == event.reverses_event_id), None)
            if target is None or target.kind is not SettlementKind.CASH_APPLIED:
                raise InvalidReversalError("reversal must reference a cash-applied event")
            already = sum(
                e.amount.cents
                for e in self.events
                if e.kind is SettlementKind.CASH_REVERSED and e.reverses_event_id == target.event_id
            )
            if already + event.amount.cents > target.amount.cents:
                raise InvalidReversalError("reversal exceeds the unreversed part of the payment")
        return replace(self, events=(*self.events, event))

    # -- position -----------------------------------------------------------------------
    def position(self, at: datetime | None = None) -> SettlementPosition:
        if at is not None:
            check_aware(at, "at")
        rev = self.in_force_revision(at)
        events = tuple(e for e in self.events if at is None or e.occurred_at <= at)
        cash = Money(self._cash_applied_cents(events), self.currency)
        concession = rev.concession
        remaining = self.obligation.face - cash - concession
        if remaining.cents == 0:
            status = SettlementStatus.SETTLED
        elif cash.cents > 0:
            status = SettlementStatus.PARTIALLY_SETTLED
        else:
            status = SettlementStatus.OPEN
        return SettlementPosition(
            original_open=self.obligation.face,
            cash_applied=cash,
            authorized_concession=concession,
            remaining_open=remaining,
            status=status,
            dispute=rev.dispute,
            revision_no=rev.revision_no,
        )


class ObligationRegistry:
    """Guards original invoice identity: one obligation per id and per provider reference."""

    def __init__(self) -> None:
        self._by_id: dict[str, Obligation] = {}
        self._by_provider: dict[tuple[str, str, str, str], str] = {}

    def register(self, obligation: Obligation) -> Obligation:
        existing = self._by_id.get(obligation.obligation_id)
        if existing is not None:
            if existing == obligation:
                return existing  # same invoice imported again: not counted twice
            raise DuplicateObligationError(f"{obligation.obligation_id} exists with other terms")
        if obligation.provider_ref is not None:
            p = obligation.provider_ref
            key = (obligation.creditor, p.provider, p.environment, p.resource_id)
            if key in self._by_provider:
                raise DuplicateObligationError(
                    f"provider invoice {p.resource_id} is already {self._by_provider[key]}"
                )
            self._by_provider[key] = obligation.obligation_id
        self._by_id[obligation.obligation_id] = obligation
        return obligation

    def __len__(self) -> int:
        return len(self._by_id)

    def total_face(self, currency: str) -> Money:
        return Money.total(
            (o.face for o in self._by_id.values() if o.currency == currency), currency
        )
