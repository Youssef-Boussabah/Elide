"""Read-only diagnosis of a held pilot intent and its "CONDITIONS CHANGED" label.

    DATABASE_URL=<pilot database url> uv run python scripts/diagnose_pilot_intent.py

For every payment intent: the authorization, the intent, its cash commitment, its outbox
messages, the in-force revision, the confirmed policy and the current evidence. It then
recomputes the authorization's material conditions twice:
- as the plan VIEW does (`load_network` without the executing intent), and
- as execution revalidation does (`load_network(executing_intent=...)`: the intent's own
  invoice included and its own reservation excluded),
and runs the independent verifier on current data. No PayPal call; every check that the
worker would make with the provider is computed locally (fingerprints only). The
connection is rolled back: nothing is written.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select, text

from api.config import get_settings
from api.execution import bindings
from api.integrations.paypal.redaction import fingerprint
from api.liquidity.store import _current_evidence
from api.liquidity.tables import capacity_commitment
from api.workflow import conditions, policies
from api.workflow.networks import load_network
from api.workflow.plans import economic_digest, load_snapshot, verify_leg_now
from api.workflow.store import load_record
from api.workflow.tables import outbox_message, payment_authorization, payment_intent


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def main() -> int:
    s = get_settings()
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    now = datetime.now(UTC)
    out: dict = {"as_of": now.isoformat(), "intents": []}
    with engine.connect() as conn:
        if conn.execute(text("SELECT current_database()")).scalar_one() != "elide_sandbox_pilot":
            print("REFUSED: not the pilot database")
            return 2
        conn.rollback()
        with conn.begin() as tx:
            for i in conn.execute(select(payment_intent)).mappings().all():
                a = (
                    conn.execute(
                        select(payment_authorization).where(
                            payment_authorization.c.authorization_id == i["authorization_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                snap = load_snapshot(conn, i["plan_id"], i["payer_merchant"])
                record = snap["record"]
                leg = next(x for x in record["selected_legs"] if x["leg_id"] == a["option_id"])
                view_net = load_network(
                    conn, snap["network_id"], initiator=record["focal_merchant"], as_of=now
                )
                exec_net = load_network(
                    conn,
                    snap["network_id"],
                    initiator=record["focal_merchant"],
                    as_of=now,
                    executing_intent=i["intent_id"],
                )
                view_cond = conditions.payment_conditions(conn, view_net, leg, now)
                exec_cond = conditions.payment_conditions(conn, exec_net, leg, now)
                ev = _current_evidence(conn, i["payer_merchant"], "USD", now)
                rec = load_record(conn, i["obligation_id"]).record
                rev = rec.in_force_revision(now)
                pol = policies.latest(conn, i["payer_merchant"])
                commits = (
                    conn.execute(
                        select(capacity_commitment).where(
                            capacity_commitment.c.merchant_id == i["payer_merchant"]
                        )
                    )
                    .mappings()
                    .all()
                )
                msgs = conn.execute(select(outbox_message)).mappings().all()
                inv_b = bindings.active(conn, i["payee_merchant"], "invoicer")
                pay_b = bindings.active(conn, i["payer_merchant"], "payer")
                cred = fingerprint(s.paypal_client_id.get_secret_value())
                email = s.paypal_sandbox_payer_email.get_secret_value().strip().lower()
                out["intents"].append(
                    {
                        "intent": {
                            k: _iso(i[k])
                            for k in (
                                "intent_id",
                                "status",
                                "status_reason",
                                "obligation_id",
                                "revision_no",
                                "amount_cents",
                                "fee_ceiling_cents",
                                "evidence_kind",
                                "commitment_id",
                                "created_at",
                                "plan_id",
                            )
                        },
                        "authorization": {
                            "authorization_id": a["authorization_id"],
                            "decision": a["decision"],
                            "decided_at": _iso(a["decided_at"]),
                            "expires_at": _iso(a["expires_at"]),
                            "expired": a["expires_at"] <= now,
                            "conditions_digest": a["conditions_digest"],
                            "revision_no": a["revision_no"],
                            "amount_cents": a["amount_cents"],
                            "fee_ceiling_cents": a["fee_ceiling_cents"],
                        },
                        "conditions_as_plan_view_computes_them": {
                            "obligation_in_network": view_net.net.has_option(leg["leg_id"]),
                            "conditions": "None (invoice excluded: in execution)"
                            if view_cond is None
                            else "computed",
                            "matches_authorization": view_cond is not None
                            and conditions.digest(view_cond) == a["conditions_digest"],
                            "economic_digest_equals_plan": economic_digest(view_net)
                            == snap["economic_digest"],
                        },
                        "conditions_as_execution_revalidates_them": {
                            "obligation_in_network": exec_net.net.has_option(leg["leg_id"]),
                            "matches_authorization": exec_cond is not None
                            and conditions.digest(exec_cond) == a["conditions_digest"],
                            "verifier_issues": verify_leg_now(exec_net, leg, now),
                            "conditions": exec_cond,
                        },
                        "in_force_revision": {
                            "no": rev.revision_no,
                            "due_at": _iso(rev.due_at),
                            "concession_cents": rev.concession.cents,
                            "open_cents": rec.position(now).remaining_open.cents,
                        },
                        "policy": None
                        if pol is None
                        else {
                            "version": pol.confirmed.version,
                            "reserve_floor_cents": pol.confirmed.policy.reserve_floor_cents,
                        },
                        "evidence": None
                        if ev is None
                        else {
                            "evidence_id": ev.evidence_id,
                            "kind": ev.kind.value,
                            "spendable_cents": ev.verified_spendable.cents,
                            "reserve_cents": ev.protected_reserve.cents,
                            "observed_at": _iso(ev.observed_at),
                            "valid_until": _iso(ev.valid_until),
                            "covers_pay_by": ev.valid_until > rev.due_at,
                        },
                        "commitments": [
                            {
                                k: _iso(c[k])
                                for k in ("commitment_id", "kind", "amount_cents", "released_at")
                            }
                            for c in commits
                        ],
                        "outbox": [
                            {
                                k: _iso(m[k])
                                for k in (
                                    "id",
                                    "kind",
                                    "intent_id",
                                    "status",
                                    "attempts",
                                    "available_at",
                                )
                            }
                            for m in msgs
                        ],
                        "bindings": {
                            "invoicer_matches_credentials": inv_b is not None
                            and inv_b.credential_fingerprint == cred,
                            "payer_matches_configured_payer": pay_b is not None
                            and pay_b.credential_fingerprint == fingerprint(email),
                        },
                    }
                )
            tx.rollback()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
