"""Material conditions of a payment authorization (ADR-0011 addendum, finding 1).

An approval is consent to pay a specific amount to a specific payee under specific
conditions. It may be reused across harmless re-analysis (time passing, other merchants'
unrelated changes) only while every condition below is unchanged; any change requires a
new approval:

- the invoice: obligation, in-force revision number AND its stored content hash, currency,
  due date and outstanding balance;
- the payee, and both parties' current participation in the network;
- the fee terms (status, payer fee, payee fee) and the leg's fee ceiling;
- the payer's newest confirmed policy (version and digest), the effective protected
  reserve max(evidence reserve, policy floor), its fee cap and whether the payee is
  prohibited;
- the evidence basis of the payer's capacity (synthetic / attested / provider-verified).

Cash amounts and other commitments are NOT conditions of consent: they are re-checked by
capacity reservation and the independent verifier every time an intent is created.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select

from api.workflow import policies
from api.workflow.networks import LoadedNetwork
from api.workflow.tables import obligation_revision

SCHEMA = "elide.payment-conditions.v1"


def payment_conditions(
    conn: Connection, loaded: LoadedNetwork, leg: dict[str, Any], at: datetime
) -> dict[str, Any] | None:
    """Current conditions for `leg` (a plan leg dict), or None if its invoice or revision is
    no longer in the permissioned network."""
    net = loaded.net
    payer, payee, oid = leg["payer"], leg["payee"], leg["obligation_id"]
    inp = next((i for i in loaded.inputs if i.record.obligation.obligation_id == oid), None)
    if inp is None:
        return None
    record = inp.record
    rev = record.in_force_revision(at)
    pos = record.position(at)
    rev_sha = conn.execute(
        select(obligation_revision.c.content_sha256).where(
            obligation_revision.c.obligation_id == oid,
            obligation_revision.c.revision_no == leg["revision_no"],
        )
    ).scalar_one_or_none()
    stored = policies.latest(conn, payer)
    p = stored.confirmed.policy if stored else None
    ev = loaded.evidence.get(payer)
    evidence_reserve = ev.protected_reserve.cents if ev is not None else 0
    floor = p.reserve_floor_cents if p and p.reserve_floor_cents is not None else 0
    pol = net.policy(payer) if payer in loaded.share_aggregates else None
    out = {
        "schema": SCHEMA,
        "obligation_id": oid,
        "revision_no": leg["revision_no"],
        "in_force_revision_no": rev.revision_no,
        "revision_sha256": rev_sha,
        "currency": record.currency,
        "due_at": rev.due_at.isoformat(),
        "open_cents": pos.remaining_open.cents,
        "payee": payee,
        "payer_participating": bool(pol and pol.participating),
        "payee_participating": payee in loaded.share_aggregates,
        "fee": {
            "status": inp.fee.status.value,
            "payer_fee_cents": inp.fee.payer_fee.cents,
            "payee_fee_cents": inp.fee.payee_fee.cents,
            "ceiling_cents": int(leg["payer_fee_cents"]),
        },
        "payer_policy": None
        if stored is None
        else {"version": stored.confirmed.version, "digest": stored.confirmed.digest},
        "effective_reserve_cents": max(evidence_reserve, floor),
        "max_fee_per_leg_cents": None
        if pol is None or pol.max_fee_per_leg is None
        else pol.max_fee_per_leg.cents,
        "payee_prohibited": bool(pol and payee in pol.prohibited_counterparties),
        "evidence_kind": ev.kind.value if ev is not None else "none",
    }
    return out


def digest(conditions: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(conditions, sort_keys=True).encode()).hexdigest()


def policy_reserve_floor(conn: Connection, merchant_id: str) -> int:
    stored = policies.latest(conn, merchant_id)
    if stored is None or stored.confirmed.policy.reserve_floor_cents is None:
        return 0
    return int(stored.confirmed.policy.reserve_floor_cents)
