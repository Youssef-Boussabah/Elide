"""Double-entry journal model (pure). Persistence lives in `api.accounting.store`.

- A `JournalEntry` is a set of `Posting`s that balances to zero **per currency**.
  A cross-currency event (e.g. PayPal converting USD net into a CAD credit) balances each
  currency separately through an FX position account; no amount is invented.
- Entries are immutable. A correction is a REVERSAL entry whose postings are the exact
  negation of the original, linked by `reverses_key`.
- Every entry carries an `idempotency_key` (one economic event = one entry) and an
  evidence class. Provider facts also carry an immutable `ProviderEventRef`.
- `content_digest()` identifies the entry's content, so replaying the same key with the
  same content is a no-op and with different content is a conflict.

Elide's journal records what the provider reported; it is not PayPal's ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from api.domain.invoice import check_aware, check_id
from api.domain.money import SignedAmount

_ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9_]*(:[A-Za-z0-9_.-]+){0,4}$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{2,199}$")


class JournalError(ValueError):
    pass


class UnbalancedEntryError(JournalError):
    pass


class EvidenceClass(StrEnum):
    SANDBOX_VERIFIED = "sandbox_verified"  # from an independently fetched provider resource
    MERCHANT_ATTESTED = "merchant_attested"
    SYNTHETIC = "synthetic"  # simulated fixture data, never a real movement


class EntryKind(StrEnum):
    INVOICE_ISSUED = "invoice_issued"
    CONCESSION_AUTHORIZED = "concession_authorized"
    PROVIDER_SETTLEMENT = "provider_settlement"
    SYNTHETIC_TRANSFER = "synthetic_transfer"
    REVERSAL = "reversal"


@dataclass(frozen=True, slots=True)
class ProviderEventRef:
    provider: str
    environment: str
    resource_type: str
    resource_id: str

    def __post_init__(self) -> None:
        for v, what in (
            (self.provider, "provider"),
            (self.environment, "environment"),
            (self.resource_type, "resource_type"),
        ):
            check_id(v, what)
        if self.environment != "sandbox":
            raise JournalError("only Sandbox provider facts are recorded in this project")
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise JournalError("provider resource_id is required")


@dataclass(frozen=True, slots=True)
class Posting:
    account: str
    amount: SignedAmount

    def __post_init__(self) -> None:
        if not isinstance(self.account, str) or not _ACCOUNT_RE.match(self.account):
            raise JournalError(f"invalid account name {self.account!r}")
        if not isinstance(self.amount, SignedAmount):
            raise JournalError("posting amount must be a SignedAmount")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    entity_id: str
    idempotency_key: str
    kind: EntryKind
    evidence_class: EvidenceClass
    description: str
    occurred_at: datetime
    postings: tuple[Posting, ...]
    provider_ref: ProviderEventRef | None = None
    reverses_key: str | None = None

    def __post_init__(self) -> None:
        check_id(self.entity_id, "entity_id")
        if not isinstance(self.idempotency_key, str) or not _KEY_RE.match(self.idempotency_key):
            raise JournalError(f"invalid idempotency key {self.idempotency_key!r}")
        if not isinstance(self.kind, EntryKind):
            raise JournalError(f"invalid entry kind {self.kind!r}")
        if not isinstance(self.evidence_class, EvidenceClass):
            raise JournalError(f"invalid evidence class {self.evidence_class!r}")
        if not self.description.strip():
            raise JournalError("description is required")
        check_aware(self.occurred_at, "occurred_at")
        if len(self.postings) < 2:
            raise UnbalancedEntryError("an entry needs at least two postings")
        for cur, total in self.totals_by_currency().items():
            if total != 0:
                raise UnbalancedEntryError(f"{cur} postings sum to {total}, not 0")
        if (self.kind is EntryKind.REVERSAL) != (self.reverses_key is not None):
            raise JournalError("exactly REVERSAL entries reference a reversed entry")
        if self.evidence_class is EvidenceClass.SANDBOX_VERIFIED and self.provider_ref is None:
            raise JournalError("a Sandbox-verified entry must cite its provider resource")
        if self.evidence_class is EvidenceClass.SYNTHETIC and self.provider_ref is not None:
            raise JournalError("synthetic entries cannot cite a provider resource")

    def totals_by_currency(self) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for p in self.postings:
            totals[p.amount.currency] += p.amount.cents
        return dict(totals)

    def content_digest(self) -> str:
        body = {
            "entity_id": self.entity_id,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind.value,
            "evidence_class": self.evidence_class.value,
            "description": self.description,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "postings": [[p.account, p.amount.currency, p.amount.cents] for p in self.postings],
            "provider_ref": None
            if self.provider_ref is None
            else [
                self.provider_ref.provider,
                self.provider_ref.environment,
                self.provider_ref.resource_type,
                self.provider_ref.resource_id,
            ],
            "reverses_key": self.reverses_key,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def reversal(self, idempotency_key: str, occurred_at: datetime, reason: str) -> JournalEntry:
        if self.kind is EntryKind.REVERSAL:
            raise JournalError("a reversal is not itself reversed; post a new entry instead")
        if occurred_at < self.occurred_at:
            raise JournalError("a reversal cannot predate the entry it reverses")
        return JournalEntry(
            entity_id=self.entity_id,
            idempotency_key=idempotency_key,
            kind=EntryKind.REVERSAL,
            evidence_class=self.evidence_class,
            description=f"Reversal of {self.idempotency_key}: {reason}",
            occurred_at=occurred_at,
            postings=tuple(Posting(p.account, -p.amount) for p in self.postings),
            provider_ref=self.provider_ref,
            reverses_key=self.idempotency_key,
        )
