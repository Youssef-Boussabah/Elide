"""Read-only diagnosis: why does the pilot plan contain no leg? (owner blocker 2026-11-12)

    DATABASE_URL=<pilot database url> uv run python scripts/diagnose_pilot_plan.py

Loads the permissioned pilot network from the database exactly as the workspace does,
then runs the P2 solver and, for every payment option and every slot up to the due slot,
the solver's own leg evaluation plus the reason a slot is refused. No PayPal call; the
connection is rolled back, so nothing is written.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

from api.config import get_settings
from api.planning.solver import SolveConfig, _Search, solve
from api.workflow.networks import load_network
from api.workflow.store import load_record


def main() -> int:
    s = get_settings()
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    now = datetime.now(UTC)
    out: dict = {"as_of": now.isoformat()}
    with engine.connect() as conn:
        db = conn.execute(text("SELECT current_database()")).scalar_one()
        if db != "elide_sandbox_pilot":
            print(f"REFUSED: {db}")
            return 2
        conn.rollback()  # end the autobegun transaction of the check above
        tx = conn.begin()
        try:
            rec = load_record(conn, "pilot_001").record
            out["revisions"] = [
                {
                    "no": r.revision_no,
                    "status": r.status.value,
                    "due_at": r.due_at.isoformat(),
                    "concession_cents": r.concession.cents,
                    "authorized_by": sorted(r.authorized_by),
                }
                for r in rec.revisions
            ]
            loaded = load_network(conn, "sandbox_pilot_network", initiator="pilot_buyer", as_of=now)
            net = loaded.net
            e = net.obligation("pilot_001")
            out["edge"] = {
                "face": e.face.cents,
                "in_force_concession": e.in_force_concession.cents,
                "revision_no": e.revision_no,
                "due_at": e.due_at.isoformat(),
                "open": e.open.cents,
            }
            pos = net.position("pilot_buyer")
            out["payer_position"] = {
                "available": pos.available.cents,
                "protected_reserve": pos.protected_reserve.cents,
                "committed": pos.committed.cents,
                "evidence_kind": pos.evidence_kind,
                "valid_until": pos.valid_until.isoformat() if pos.valid_until else None,
            }
            pol = net.policy("pilot_buyer")
            out["payer_policy"] = {
                "min_early_discount": pol.min_early_discount.cents,
                "hurdle_bps": pol.hurdle_bps,
                "min_net_benefit_cents": pol.min_net_benefit_cents,
                "source": pol.source,
            }
            out["slots"] = [{"t": t, "start": net.clock.start(t).isoformat()} for t in range(0, 6)]
            search = _Search(net, SolveConfig())
            due = search.due["pilot_001"]
            out["due_slot"] = due
            opts = []
            for o in net.options_for("pilot_001"):
                allowed = search._usable(o)
                per_slot = []
                for t in range(0, (due or 0) + 2):
                    start = net.clock.start(t)
                    reasons = []
                    if o.expires_at is not None and not start < o.expires_at:
                        reasons.append("OPTION_EXPIRED")
                    if pos.valid_until is not None and not start < pos.valid_until:
                        reasons.append("CAPACITY_EVIDENCE_EXPIRED")
                    early = due is not None and t < due
                    extra = o.concession.cents - e.in_force_concession.cents
                    if early and extra < pol.min_early_discount.cents:
                        reasons.append(
                            f"EARLY_NEEDS_EXTRA_DISCOUNT(extra={extra}"
                            f"<min={pol.min_early_discount.cents})"
                        )
                    if due is not None and t > due:
                        reasons.append("LATE")
                    ev = search._eval(o, t)
                    per_slot.append(
                        {"t": t, "early": early, "feasible": ev is not None, "reasons": reasons}
                    )
                opts.append(
                    {
                        "option_id": o.option_id,
                        "terms": o.terms.value,
                        "cash": o.cash.cents,
                        "concession": o.concession.cents,
                        "usable": allowed,
                        "slots": per_slot,
                    }
                )
            out["options"] = opts
            res = solve(net, SolveConfig())
            out["solver"] = {
                "status": res.status.value,
                "legs": [(x.leg_id, x.slot) for x in res.schedule],
            }
        finally:
            tx.rollback()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
