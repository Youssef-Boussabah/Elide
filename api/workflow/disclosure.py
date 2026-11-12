"""Explicit disclosure policy and allowlisted merchant views (ADR-0011 addendum, finding 2).

Views are BUILT from allowlisted fields of the merchant's own data; nothing is copied from
the privileged network report and then deleted. Network-level information (totals,
baseline comparisons, counts of legs, approvals, bottlenecks or participants, and any
recommendation wording derived from other merchants' data) is added only when
`decide()` allows it:

- the viewer is the initiating (focal) merchant, AND
- every other active participant has consented to aggregate disclosure, AND
- at least MIN_GROUP other active participants exist (small-group suppression, doc 11).

Every HTTP response and every model prompt is built from these views only, so the same
decision applies everywhere. Merchants that are not active participants never appear:
the planner never receives their invoices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.opportunities.analysis import merchant_view as p3_merchant_view
from api.planning import plan as p2_plan

MIN_GROUP = 3
VIEW_SCHEMA = "elide.workspace-opportunity.v2"
PLAN_SCHEMA = "elide.workspace-plan.v2"

CONDITIONS = (
    "every payer and counterparty must approve its own payment; nothing happens without "
    "your own approval",
    "downstream receipts are simulated; no downstream funds are verified spendable",
)
EXECUTION = {
    "may_execute": False,
    "reason": "read-only simulation: no payment authorization or spendable downstream funds exist",
}
_ACTION_KEYS = (
    "leg_id",
    "obligation_id",
    "payer",
    "payee",
    "amount_cents",
    "slot_start",
    "on_time",
)
_BOTTLENECK_KEYS = (
    "obligation_id",
    "debtor",
    "creditor",
    "face_cents",
    "open_cents",
    "due_at",
    "reason_code",
    "reason",
    "unlocked_by_plan",
)
_HINT_KEYS = (
    "kind",
    "obligation_id",
    "payer",
    "current_offer_cents",
    "minimum_needed_cents",
    "detail",
)
_BENEFIT_KEYS = (
    "discount_gained_cents",
    "discount_given_cents",
    "fees_paid",
    "opportunity_cost_cents",
    "early_receipt_value_cents",
    "other_costs_cents",
    "net_benefit",
)
_PLAN_LEG_KEYS = (
    "leg_id",
    "obligation_id",
    "revision_no",
    "terms",
    "payer",
    "payee",
    "slot",
    "slot_start",
    "amount_cents",
    "concession_cents",
    "on_time",
)


@dataclass(frozen=True, slots=True)
class Disclosure:
    aggregates: bool
    status: str  # SHOWN / WITHHELD_NOT_INITIATOR / WITHHELD_NO_CONSENT / WITHHELD_SMALL_GROUP
    explanation: str


def decide(viewer: str, focal: str, share_aggregates: dict[str, bool]) -> Disclosure:
    others = {m: ok for m, ok in share_aggregates.items() if m != viewer}
    if viewer != focal:
        return Disclosure(
            False,
            "WITHHELD_NOT_INITIATOR",
            "network figures are shown only to the merchant that initiated the analysis",
        )
    if not all(others.values()):
        return Disclosure(
            False,
            "WITHHELD_NO_CONSENT",
            "network figures are withheld: not "
            "every participant has agreed to contribute to aggregates",
        )
    if len(others) < MIN_GROUP:
        return Disclosure(
            False,
            "WITHHELD_SMALL_GROUP",
            "network figures are withheld: too "
            "few participants to keep them from revealing anyone's data",
        )
    return Disclosure(True, "SHOWN", "network figures shown with every participant's consent")


def _pick(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: d[k] for k in keys if k in d}


def _usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}USD {c // 100:,}.{c % 100:02d}"


def _personal(me: str, actions, bottlenecks, hints, benefit, unjustified: bool):
    """A recommendation from the viewer's own data only (no network figures)."""
    if hints:
        oid = hints[0]["obligation_id"]
        return {
            "action": "NEGOTIATE",
            "reason": f"a bounded discount counteroffer on invoice "
            f"{oid} could make it payable under your own rules",
        }
    if actions:
        pays = sum(1 for a in actions if a["payer"] == me)
        gets = len(actions) - pays
        parts = [
            p
            for p in (
                f"make {pays} payment(s)" if pays else "",
                f"receive {gets} payment(s)" if gets else "",
            )
            if p
        ]
        net = int(benefit.get("net_benefit", 0) or 0)
        reason = (
            f"you would {' and '.join(parts)} in this simulated plan; your modeled net "
            f"benefit is {_usd(net)}; nothing happens without your own approval"
        )
        if net < 0 and unjustified:
            return {
                "action": "REVIEW_ECONOMICS",
                "reason": reason + "; you have not declared a benefit that offsets this loss",
            }
        return {"action": "REVIEW_AND_DECIDE", "reason": reason}
    if bottlenecks:
        return {
            "action": "NO_ACTION",
            "reason": "nothing you can change improves your "
            "invoices right now; the blocking reasons are listed",
        }
    return {
        "action": "NOTHING_FOR_YOU",
        "reason": "you have no payment to make or receive in this plan",
    }


def opportunity_view(report: dict[str, Any], viewer: str, d: Disclosure) -> dict[str, Any]:
    base = p3_merchant_view(report, viewer)  # already merchant-scoped; used as a source only
    actions = []
    for a in base["my_actions"]:
        item = {"action": a["action"], **_pick(a, _ACTION_KEYS)}
        if a["action"] == "pay":  # the payer's own dependency, never shown to its payee
            item["depends_on_other_payments"] = bool(a.get("depends_on_other_payments"))
        actions.append(item)
    bottlenecks = [_pick(b, _BOTTLENECK_KEYS) for b in base["my_bottlenecks"]]
    hints = [_pick(h, _HINT_KEYS) for h in base["my_negotiation_hints"]]
    benefit = _pick(base["my_benefit"] or {}, _BENEFIT_KEYS) if actions else {}
    own_flags = [
        {"code": f["code"], "merchant_id": viewer, "detail": f["detail"]}
        for f in base["my_flags"]
        if f.get("merchant_id") == viewer and f["code"] != "MY_APPROVALS_OUTSTANDING"
    ]
    unjustified = viewer in report["decision"]["unjustified_modeled_losses"]
    view: dict[str, Any] = {
        "schema_version": VIEW_SCHEMA,
        "status": report["status"],
        "viewer": viewer,
        "my_actions": actions,
        "my_bottlenecks": bottlenecks,
        "my_benefit": benefit,
        "my_negotiation_hints": hints,
        "my_flags": own_flags,
        "conditions": list(CONDITIONS),
        "execution": dict(EXECUTION),
        "aggregates": {"status": d.status, "explanation": d.explanation},
    }
    if d.aggregates:
        dec = report["decision"]
        view["recommendation"] = dict(report["recommendation"])
        view["my_flags"] += [
            {"code": f["code"], "merchant_id": None, "detail": f["detail"]}
            for f in report["flags"]
            if f.get("merchant_id") is None
        ]
        view["decision"] = {
            "feasible": dec["feasible"],
            "advantage": dec["advantage"],
            "buyer_net_benefit_cents": dec["buyer_net_benefit_cents"],
            "participants_with_undeclared_losses": len(dec["unjustified_modeled_losses"]),
            "uncertainty": list(dec["uncertainty"]),
            "consent": dict(dec["consent"]),
        }
        view["comparison"] = dict(report["comparison"])
    else:
        view["recommendation"] = _personal(
            viewer, actions, bottlenecks, hints, benefit, unjustified
        )
        view["decision"] = {
            "my_part_in_plan": bool(actions),
            "my_net_benefit_cents": int(benefit.get("net_benefit", 0) or 0) if actions else None,
            "my_payments_to_approve": sum(1 for a in actions if a["payer"] == viewer),
        }
    return view


def plan_view(record: dict[str, Any], viewer: str, d: Disclosure) -> dict[str, Any]:
    base = p2_plan.merchant_view(record, viewer)  # merchant-scoped source only
    by_leg = {x["leg_id"]: x for x in record["selected_legs"]}
    legs = []
    for leg in base["my_legs"]:
        full = by_leg[leg["leg_id"]]
        item = _pick(full, _PLAN_LEG_KEYS)
        visible = {x["leg_id"] for x in base["my_legs"]}
        item["depends_on"] = [x for x in full["depends_on"] if x in visible]
        if full["payer"] == viewer:  # the payer's own fee and dependency
            item["payer_fee_cents"] = full["payer_fee_cents"]
            item["has_upstream_dependency"] = any(x not in visible for x in full["depends_on"])
        if full["payee"] == viewer:
            item["payee_fee_cents"] = full["payee_fee_cents"]
        legs.append(item)
    view: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "plan_id": record["plan_id"],
        "status": record["status"],
        "viewer": viewer,
        "as_of": record["as_of"],
        "expires_at": record["expires_at"],
        "solver_status": record["solver"]["status"],
        "my_legs": legs,
        "my_benefit": _pick(base["my_benefit"] or {}, _BENEFIT_KEYS) if legs else {},
        "my_reservation": [{"cents": r["cents"]} for r in base["my_reservation"]],
        "execution": dict(EXECUTION),
        "aggregates": {"status": d.status, "explanation": d.explanation},
    }
    if d.aggregates:
        view["other_legs_in_plan"] = len(record["selected_legs"]) - len(legs)
    return view
