"""Build the permissioned planning network from PostgreSQL (ADR-0011 §9).

Only merchants currently participating in the network, and only invoices between two
participants, are included ("permissioned edges only", doc 11). Capacity comes from the
P1 evidence store at `as_of` (current evidence + active commitments): expired or missing
evidence counts for zero, receivables add nothing until verified available. Policies are
the merchants' newest confirmed revisions, applied with the P3 `apply_confirmed_policy`
(only ever tighter). Nothing here writes to the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, select

from api.domain.money import USD, Money
from api.liquidity.capacity import CapacityEvidence, compute_capacity
from api.liquidity.store import _active_commitments, _current_evidence
from api.planning.builder import InvoiceInput, build_network, position_from_evidence
from api.planning.network import (
    SYNTHETIC_ZERO_LAG,
    AvailabilityModel,
    MerchantPolicy,
    MerchantPosition,
    Network,
    SlotClock,
)
from api.policy.service import apply_confirmed_policy
from api.workflow import policies
from api.workflow.store import (
    NotVisibleError,
    active_participants,
    load_record,
    network_obligation_ids,
)
from api.workflow.tables import network as network_table
from api.workflow.tables import payment_intent

# Every non-terminal intent state (and `exception`, which keeps its cash): the invoice is
# in execution and is not planned again. Equal to api.execution.states.IN_EXECUTION (test).
ACTIVE_INTENT = (
    "approved",
    "session_pending",
    "payer_action_pending",
    "provider_pending",
    "provider_unknown",
    "provider_confirmed",
    "exception",
)

SLOTS = 31
SLOT = timedelta(days=1)
# Outside synthetic networks no receipt is assumed usable inside the horizon.
NO_REUSE = AvailabilityModel("incoming_not_usable_in_horizon", SLOTS + 1)


@dataclass(frozen=True, slots=True)
class LoadedNetwork:
    net: Network
    is_synthetic: bool
    share_aggregates: dict[str, bool]
    evidence: dict[str, CapacityEvidence | None]
    policy_versions: dict[str, int | None]
    planning_assumptions: dict[str, dict[str, Any]]
    inputs: tuple[InvoiceInput, ...] = ()
    in_execution: frozenset[str] = frozenset()


def _no_evidence(merchant_id: str) -> MerchantPosition:
    zero = Money(0, USD)
    return MerchantPosition(
        merchant_id=merchant_id,
        available=zero,
        protected_reserve=zero,
        committed=zero,
        evidence_id=f"none:{merchant_id}",
        evidence_kind="unknown",
        valid_until=None,
        notes=("no current capacity evidence: counts for zero",),
    )


def load_network(
    conn: Connection,
    network_id: str,
    *,
    initiator: str,
    as_of: datetime,
    executing_intent: str | None = None,
) -> LoadedNetwork:
    """The permissioned network at `as_of`.

    `executing_intent` (P5 revalidation, ADR-0012 §5): re-check one intent that is already
    in execution. Its invoice is included and its own cash reservation is excluded, so
    the independent verifier judges the payment exactly as when the intent was created,
    against everything else that has changed since."""
    row = (
        conn.execute(select(network_table).where(network_table.c.network_id == network_id))
        .mappings()
        .one_or_none()
    )
    share = active_participants(conn, network_id)
    if row is None or initiator not in share:
        raise NotVisibleError("not found")
    synthetic = bool(row["is_synthetic"])
    clock = SlotClock.uniform(as_of, SLOT, SLOTS)
    own = None
    if executing_intent is not None:
        own = (
            conn.execute(
                select(payment_intent.c.obligation_id, payment_intent.c.commitment_id).where(
                    payment_intent.c.intent_id == executing_intent
                )
            )
            .mappings()
            .one()
        )

    positions, evidence = [], {}
    for m in share:
        ev = _current_evidence(conn, m, USD, as_of)
        evidence[m] = ev
        if ev is None:
            positions.append(_no_evidence(m))
            continue
        positions.append(
            position_from_evidence(
                ev,
                as_of=as_of,
                clock=clock,
                commitments=[
                    c
                    for c in _active_commitments(conn, m)
                    if own is None or c.commitment_id != own["commitment_id"]
                ],
                allow_synthetic=synthetic,
            )
        )

    stored = {m: policies.latest(conn, m) for m in share}
    base_policies = []
    for m in share:
        s = stored[m]
        hurdle = int((s.planning_assumptions if s else {}).get("hurdle_bps", 0))
        base_policies.append(
            MerchantPolicy(
                m,
                hurdle_bps=hurdle,
                source="no confirmed policy" if s is None else "confirmed policy",
            )
        )

    # Invoices already in execution (an intent holds their cash) are not planned again:
    # their reserved cash stays committed, and anything funded by them waits for provider
    # confirmation and verified availability (D006).
    executing = set(
        conn.execute(
            select(payment_intent.c.obligation_id).where(payment_intent.c.status.in_(ACTIVE_INTENT))
        ).scalars()
    )
    if own is not None:
        executing.discard(own["obligation_id"])
    invoices = []
    for oid in network_obligation_ids(conn, network_id):
        if oid in executing:
            continue
        inv = load_record(conn, oid)
        ob = inv.record.obligation
        if ob.debtor in share and ob.creditor in share and ob.issued_at <= as_of:
            invoices.append(InvoiceInput(inv.record, inv.fee, inv.provider_ref))

    labels = (f"NETWORK:{network_id}", "PERSISTED_INPUTS")
    if synthetic:
        labels = ("SYNTHETIC_NETWORK", *labels)
    net = build_network(
        network_id,
        as_of=as_of,
        clock=clock,
        availability=SYNTHETIC_ZERO_LAG if synthetic else NO_REUSE,
        focal_merchant=initiator,
        positions=positions,
        policies=base_policies,
        invoices=invoices,
        labels=labels,
    )
    for s in stored.values():
        if s is not None:
            net = apply_confirmed_policy(net, s.confirmed, as_of)
    return LoadedNetwork(
        net=net,
        is_synthetic=synthetic,
        share_aggregates=share,
        evidence=evidence,
        policy_versions={m: (s.confirmed.version if s else None) for m, s in stored.items()},
        planning_assumptions={m: (s.planning_assumptions if s else {}) for m, s in stored.items()},
        inputs=tuple(invoices),
        in_execution=frozenset(executing),
    )


BASIS = {
    "provider_verified": "PROVIDER_VERIFIED",
    "merchant_attested": "MERCHANT_ATTESTED",
    "synthetic": "SYNTHETIC",
    "unknown": "UNVERIFIED",
}


def capacity_summary(
    conn: Connection, merchant_id: str, as_of: datetime, *, synthetic: bool
) -> dict[str, Any]:
    """The merchant's OWN capacity, with its evidence basis and timestamps."""
    ev = _current_evidence(conn, merchant_id, USD, as_of)
    if ev is None:
        return {
            "basis": "NONE",
            "status": "NO_EVIDENCE",
            "deployable_cents": 0,
            "explanation": "no capacity evidence recorded; nothing is deployable",
        }
    snap = compute_capacity(
        ev, _active_commitments(conn, merchant_id), (), at=as_of, allow_synthetic=synthetic
    )
    return {
        "basis": BASIS[ev.kind.value],
        "status": snap.status.value.upper(),
        "evidence_id": ev.evidence_id,
        "observed_at": ev.observed_at.isoformat(),
        "valid_until": ev.valid_until.isoformat(),
        "currency": snap.currency,
        "verified_spendable_cents": snap.verified_spendable.cents,
        "protected_reserve_cents": snap.protected_reserve.cents,
        "committed_cents": snap.committed.cents,
        "held_cents": snap.held.cents,
        "pending_cents": snap.pending.cents,
        "deployable_cents": snap.deployable.cents,
        "reasons": list(snap.reasons),
        "explanation": (
            "deployable = max(0, verified spendable - protected reserve - committed); "
            "receivables and provider-confirmed payments add nothing until verified available"
        ),
    }
