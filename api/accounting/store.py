"""PostgreSQL persistence for the journal and the provider event inbox.

Two deduplication layers, kept separate on purpose:
- **Inbox** (`record_provider_event`): "have we seen this provider notification?"
  Keyed by (provider, environment, provider_event_id). A duplicate webhook is recorded
  once; it never posts money by itself.
- **Economic posting** (`post_entry`): "has this economic event been booked?"
  Keyed by the entry's idempotency key, plus a unique index on (provider resource, kind)
  so the same capture cannot be booked twice under two different keys.

Related entries (e.g. the invoice, discount and capture entries of one settlement) are
booked with `post_entries`, in **one** transaction: a crash between them books nothing.
`bundle_status` derives whether a bundle is completely booked from the rows themselves;
there is no stored "reconciled" flag that could disagree with the journal.

Every state-changing call runs in exactly one database transaction. Concurrent callers
with the same key serialize on the unique index: one inserts, the others observe the
committed row and return DUPLICATE (or a conflict if the content differs). Nothing is
ever updated or deleted; corrections are reversal entries (the database refuses UPDATE/
DELETE on these tables).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from api.accounting.journal import (
    EvidenceClass,
    JournalEntry,
    JournalError,
    ProviderEventRef,
    check_reversal_evidence,
)
from api.accounting.tables import journal_entry, journal_posting, provider_event_inbox


class PostStatus(StrEnum):
    POSTED = "posted"
    DUPLICATE = "duplicate"  # same key, same content: already booked, nothing added


@dataclass(frozen=True, slots=True)
class PostResult:
    status: PostStatus
    entry_id: int
    idempotency_key: str


class IdempotencyConflictError(JournalError):
    """Same idempotency key replayed with different content."""


class DuplicateProviderEventError(JournalError):
    """The provider resource already has an entry of this kind under another key."""


class ReversalError(JournalError):
    pass


class BundleStatus(StrEnum):
    NOT_IMPORTED = "not_imported"  # none of the bundle's entries is booked
    PARTIAL = "partial"  # some are booked: NOT reconciled
    COMPLETE = "complete"  # every entry booked with exactly the expected content
    CONFLICT = "conflict"  # a key is booked with different content: needs investigation


class InboxStatus(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"


class InboxConflictError(ValueError):
    """Same provider event id delivered with a different payload."""


@dataclass(frozen=True, slots=True)
class InboxResult:
    status: InboxStatus
    inbox_id: int


def _constraint(exc: IntegrityError) -> str:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", "") or ""


# -- journal ---------------------------------------------------------------------------
def post_entry(engine: Engine, entry: JournalEntry) -> PostResult:
    """Book `entry` exactly once. Safe to call concurrently and to retry."""
    return post_entries(engine, (entry,))[0]


def post_entries(engine: Engine, entries: Sequence[JournalEntry]) -> tuple[PostResult, ...]:
    """Book a related bundle atomically: all entries in one transaction, or none.

    Entries already booked with identical content are reported DUPLICATE, so a retry (or
    the completion of a bundle that was partly booked one entry at a time) is safe. Any
    conflict or failure rolls back the whole bundle.
    """
    entries = tuple(entries)
    if not entries:
        raise JournalError("an empty bundle posts nothing")
    keys = [e.idempotency_key for e in entries]
    if len(set(keys)) != len(keys):
        raise JournalError("a bundle cannot contain the same idempotency key twice")
    current: JournalEntry = entries[0]
    try:
        with engine.begin() as conn:
            results = []
            for current in entries:
                results.append(_post(conn, current))
            return tuple(results)
    except IntegrityError as exc:
        name = _constraint(exc)
        if name == "uq_journal_entry_provider_event":
            raise DuplicateProviderEventError(
                f"{current.provider_ref} already has a {current.kind.value} entry"
            ) from exc
        if name == "uq_journal_entry_reverses":
            raise ReversalError(f"{current.reverses_key} has already been reversed") from exc
        raise


def bundle_status(engine: Engine, entries: Sequence[JournalEntry]) -> BundleStatus:
    """Is this bundle completely booked? Derived from the journal rows and digests."""
    expected = {e.idempotency_key: e.content_digest() for e in entries}
    with engine.connect() as conn:
        rows = conn.execute(
            select(journal_entry.c.idempotency_key, journal_entry.c.content_sha256).where(
                journal_entry.c.idempotency_key.in_(list(expected))
            )
        ).all()
    if any(expected[k] != d for k, d in rows):
        return BundleStatus.CONFLICT
    if not rows:
        return BundleStatus.NOT_IMPORTED
    return BundleStatus.COMPLETE if len(rows) == len(expected) else BundleStatus.PARTIAL


def _post(conn: Connection, entry: JournalEntry) -> PostResult:
    reverses_id = None
    if entry.reverses_key is not None:
        reverses_id = _check_reversal(conn, entry)
    digest = entry.content_digest()
    ref = entry.provider_ref
    row = conn.execute(
        insert(journal_entry)
        .values(
            entity_id=entry.entity_id,
            idempotency_key=entry.idempotency_key,
            kind=entry.kind.value,
            evidence_class=entry.evidence_class.value,
            description=entry.description,
            occurred_at=entry.occurred_at,
            provider=ref.provider if ref else None,
            provider_environment=ref.environment if ref else None,
            provider_resource_type=ref.resource_type if ref else None,
            provider_resource_id=ref.resource_id if ref else None,
            reverses_entry_id=reverses_id,
            content_sha256=digest,
        )
        # No conflict target: every unique index (idempotency key, provider event, reversal
        # link) is an arbiter, so a concurrent writer is waited for instead of surfacing as
        # a unique-violation error. Which rule was hit is decided below.
        .on_conflict_do_nothing()
        .returning(journal_entry.c.id)
    ).first()
    if row is None:
        existing = conn.execute(
            select(journal_entry.c.id, journal_entry.c.content_sha256).where(
                journal_entry.c.idempotency_key == entry.idempotency_key
            )
        ).first()
        if existing is None:  # another key already holds this provider event or reversal
            if entry.reverses_key is not None and _already_reversed(conn, reverses_id):
                raise ReversalError(f"{entry.reverses_key} has already been reversed")
            raise DuplicateProviderEventError(
                f"{entry.provider_ref} already has a {entry.kind.value} entry"
            )
        if existing.content_sha256 != digest:
            raise IdempotencyConflictError(
                f"key {entry.idempotency_key} was already used for different content"
            )
        return PostResult(PostStatus.DUPLICATE, existing.id, entry.idempotency_key)
    entry_id = row.id
    conn.execute(
        journal_posting.insert(),
        [
            {
                "entry_id": entry_id,
                "line_no": i,
                "account": p.account,
                "currency": p.amount.currency,
                "amount_cents": p.amount.cents,
            }
            for i, p in enumerate(entry.postings, start=1)
        ],
    )
    return PostResult(PostStatus.POSTED, entry_id, entry.idempotency_key)


def _already_reversed(conn: Connection, target_id: int | None) -> bool:
    if target_id is None:
        return False
    return (
        conn.execute(
            select(journal_entry.c.id).where(journal_entry.c.reverses_entry_id == target_id)
        ).first()
        is not None
    )


def _check_reversal(conn: Connection, entry: JournalEntry) -> int:
    j = journal_entry.c
    target = conn.execute(
        select(
            j.id,
            j.kind,
            j.entity_id,
            j.evidence_class,
            j.provider,
            j.provider_environment,
            j.provider_resource_type,
            j.provider_resource_id,
        ).where(j.idempotency_key == entry.reverses_key)
    ).first()
    if target is None:
        raise ReversalError(f"entry {entry.reverses_key} does not exist")
    if target.kind == "reversal":
        raise ReversalError("a reversal cannot be reversed")
    if target.entity_id != entry.entity_id:
        raise ReversalError("a reversal must belong to the same accounting entity")
    target_ref = (
        None
        if target.provider is None
        else ProviderEventRef(
            target.provider,
            target.provider_environment,
            target.provider_resource_type,
            target.provider_resource_id,
        )
    )
    try:
        check_reversal_evidence(
            EvidenceClass(target.evidence_class),
            target_ref,
            entry.evidence_class,
            entry.provider_ref,
        )
    except JournalError as exc:
        raise ReversalError(str(exc)) from exc
    original = conn.execute(
        select(
            journal_posting.c.account, journal_posting.c.currency, journal_posting.c.amount_cents
        )
        .where(journal_posting.c.entry_id == target.id)
        .order_by(journal_posting.c.line_no)
    ).all()
    expected = [(a, c, -n) for a, c, n in original]
    actual = [(p.account, p.amount.currency, p.amount.cents) for p in entry.postings]
    if actual != expected:
        raise ReversalError("reversal postings must exactly negate the original entry")
    return target.id


def account_balances(
    engine_or_conn: Engine | Connection, entity_id: str
) -> dict[tuple[str, str], int]:
    """Signed balance per (account, currency) for one entity (debit positive)."""
    stmt = (
        select(
            journal_posting.c.account,
            journal_posting.c.currency,
            func.sum(journal_posting.c.amount_cents),
        )
        .join(journal_entry, journal_entry.c.id == journal_posting.c.entry_id)
        .where(journal_entry.c.entity_id == entity_id)
        .group_by(journal_posting.c.account, journal_posting.c.currency)
    )
    conn_ctx = engine_or_conn.connect() if isinstance(engine_or_conn, Engine) else None
    conn = conn_ctx if conn_ctx is not None else engine_or_conn
    try:
        rows = conn.execute(stmt).all()  # type: ignore[union-attr]
    finally:
        if conn_ctx is not None:
            conn_ctx.close()
    return {(a, c): int(s) for a, c, s in rows if int(s) != 0}


def trial_balance(engine: Engine) -> dict[str, int]:
    """Sum of all postings per currency across the whole journal. Must be all zeros."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(journal_posting.c.currency, func.sum(journal_posting.c.amount_cents)).group_by(
                journal_posting.c.currency
            )
        ).all()
    out: dict[str, int] = defaultdict(int)
    for cur, total in rows:
        out[cur] = int(total)
    return dict(out)


def entry_count(engine: Engine, idempotency_key_prefix: str = "") -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(journal_entry)
                .where(journal_entry.c.idempotency_key.startswith(idempotency_key_prefix))
            ).scalar_one()
        )


# -- provider event inbox ---------------------------------------------------------------
def payload_digest(payload: Any) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def record_provider_event(
    engine: Engine,
    *,
    provider: str,
    environment: str,
    provider_event_id: str,
    event_type: str,
    payload: Any,
) -> InboxResult:
    """Record a provider notification once. Does not post money (no webhook worker yet)."""
    digest = payload_digest(payload)
    with engine.begin() as conn:
        row = conn.execute(
            insert(provider_event_inbox)
            .values(
                provider=provider,
                environment=environment,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payload_sha256=digest,
            )
            .on_conflict_do_nothing(constraint="uq_provider_event_inbox_event")
            .returning(provider_event_inbox.c.id)
        ).first()
        if row is not None:
            return InboxResult(InboxStatus.NEW, row.id)
        existing = conn.execute(
            select(provider_event_inbox.c.id, provider_event_inbox.c.payload_sha256).where(
                provider_event_inbox.c.provider == provider,
                provider_event_inbox.c.environment == environment,
                provider_event_inbox.c.provider_event_id == provider_event_id,
            )
        ).one()
    if existing.payload_sha256 != digest:
        raise InboxConflictError(f"event {provider_event_id} redelivered with a different payload")
    return InboxResult(InboxStatus.DUPLICATE, existing.id)
