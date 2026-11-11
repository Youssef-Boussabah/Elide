"""Proactive, read-only opportunity analysis over a P2 network (SYNTHETIC scope).

Starting only from the network and its financial evidence, this module:
- runs the verified P2 plan (B4) and the B0-B5 baselines;
- lists bottlenecks: invoices not paid on time without coordination (B0), with the
  deterministic reason (capacity, the payer's or payee's own rules, expired offer,
  currency-blocked or unverified/held funds, dispute) and whether the plan unlocks them;
- recommends one of: RECOMMEND_PLAN (more paid on time than doing nothing),
  NEGOTIATE (no improvement today, but a bounded discount counteroffer could unlock an
  invoice), NO_ACTION (nothing to gain; the honest default), BLOCKED (nothing can be paid
  and no bounded change helps);
- reports increments against B0 (do nothing), B1 (earliest-due-first) and B3 (random
  buyer tie-break), and says plainly when a simple rule already matches the plan;
- flags supplier modeled losses, uncertain fees, unavailable or blocked funds, the buyer
  earning less than its own best choice, synthetic approvals that are still outstanding,
  and simulated downstream legs.

`merchant_view` scopes the report to one merchant (its own legs, bottlenecks, benefit and
flags; counts only for the rest) and `facts` turns that view into numbered facts for the
explanation layer. Nothing here changes an invoice, a reserve, a commitment or an approval.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from api.planning.network import FeeStatus, Network, TermsKind
from api.planning.plan import build_plan

OPPORTUNITY_SCHEMA = "elide.opportunities.v1"
_REASON_ORDER = (
    "CURRENCY_BLOCKED",
    "DISPUTED",
    "CONSENT_REFUSED",
    "NOT_PARTICIPATING",
    "PAYEE_REJECTS",
    "PAYER_REJECTS",
    "OFFER_EXPIRED",
    "EVIDENCE_EXPIRED",
    "CAPACITY",
)


def _usd(cents: int | Fraction) -> str:
    c = round(cents)
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}USD {c // 100:,}.{c % 100:02d}"


def _frac_cents(s: str) -> Fraction:
    return Fraction(s)


def _cents_str(x: Fraction) -> str:
    """Exact value, plus a rounded cents string for display."""
    return str(x) if x.denominator == 1 else f"{x} (~{float(x):.2f})"


def _bottleneck_reason(net: Network, plan: dict[str, Any], oid: str) -> tuple[str, str]:
    edge = net.obligation(oid)
    excl = [x for x in net.exclusions if x.obligation_id == oid]
    if edge.currency != "USD":
        return "CURRENCY_BLOCKED", f"{edge.currency} invoice; USD-only planning (D008)"
    rejected = {r["leg_id"]: r for r in plan["rejected_legs"]}
    # the reason that blocks the BEST available terms (largest concession) is the real one
    current = [o for o in net.options_for(oid) if o.terms is not TermsKind.NEGOTIATED_PROPOSAL]
    options = sorted(current, key=lambda o: (-o.concession.cents, o.option_id))
    reasons = [rejected[o.option_id] for o in options if o.option_id in rejected]
    reasons += [{"code": x.code, "explanation": x.explanation} for x in excl]
    if not reasons:
        return "NOT_SCHEDULED", "no leg for this invoice was scheduled on time"
    if excl and not options:
        reasons.sort(
            key=lambda r: _REASON_ORDER.index(r["code"]) if r["code"] in _REASON_ORDER else 99
        )
    first = reasons[0]
    payer = net.position(edge.debtor)
    if first["code"] == "CAPACITY":
        if payer.unverified or payer.blocked:
            return "FUNDS_UNAVAILABLE", first["explanation"]
        if any(x["payer"] == edge.debtor for x in plan["selected_legs"]):
            return "COMPETES_WITH_PLAN", (
                "the payer's cash is already used by the recommended plan: " + first["explanation"]
            )
    return first["code"], first["explanation"]


def _negotiation_hint(net: Network, oid: str) -> dict[str, Any] | None:
    """Smallest concession that would satisfy the payer's own minimum and cash."""
    edge = net.obligation(oid)
    offers = [o for o in net.options_for(oid) if o.terms is TermsKind.OPEN_OFFER]
    if not offers:
        return None
    pol, pos = net.policy(edge.debtor), net.position(edge.debtor)
    best = max(o.concession.cents - edge.in_force_concession.cents for o in offers)
    need = max(pol.min_early_discount.cents, edge.open.cents - pos.base_slack_cents)
    if best >= need or need >= edge.open.cents:
        return None  # the offer already fits the payer's rules and cash (or nothing can)
    return {
        "kind": "DISCOUNT_COUNTEROFFER",
        "obligation_id": oid,
        "payer": edge.debtor,
        "payee": edge.creditor,
        "current_offer_cents": best,
        "minimum_needed_cents": need,
    }


def analyze(net: Network) -> dict[str, Any]:
    plan = build_plan(net)
    baselines = {b["baseline_id"]: b for b in plan["baselines"]}
    on_time = {k: _frac_cents(b["expected_on_time_face_cents"]) for k, b in baselines.items()}
    b0_paid = set()
    for leg in baselines["B0"]["schedule"]:
        oid = net.option(leg.split("@")[0]).obligation_id
        slot = int(leg.split("@")[1])
        due = net.clock.last_slot_at_or_before(net.obligation(oid).due_at)
        if due is not None and slot <= due:
            b0_paid.add(oid)
    plan_paid = {x["obligation_id"] for x in plan["selected_legs"] if x["on_time"]}

    bottlenecks = []
    for e in sorted(net.obligations, key=lambda e: e.obligation_id):
        if e.obligation_id in b0_paid:
            continue
        code, detail = _bottleneck_reason(net, plan, e.obligation_id)
        bottlenecks.append(
            {
                "obligation_id": e.obligation_id,
                "debtor": e.debtor,
                "creditor": e.creditor,
                "face_cents": e.face.cents,
                "open_cents": e.open.cents,
                "due_at": e.due_at.isoformat(),
                "without_coordination": "not paid on time",
                "reason_code": "UNLOCKED_BY_PLAN" if e.obligation_id in plan_paid else code,
                "reason": "the recommended plan pays it on time"
                if e.obligation_id in plan_paid
                else detail,
                "unlocked_by_plan": e.obligation_id in plan_paid,
            }
        )

    hints = [
        h
        for b in bottlenecks
        if not b["unlocked_by_plan"]
        and b["reason_code"] in ("PAYER_REJECTS", "CAPACITY")
        and (h := _negotiation_hint(net, b["obligation_id"])) is not None
    ]
    if on_time["B5"] > on_time["B4"]:
        hints.append(
            {
                "kind": "PENDING_PROPOSAL",
                "payer": focal_id(net),
                "obligation_id": None,
                "detail": "a negotiated proposal already on file would pay "
                f"{_usd(on_time['B5'] - on_time['B4'])} more on time if both parties sign it",
            }
        )
    gain_b0 = on_time["B4"] - on_time["B0"]
    if plan["solver"]["status"] not in ("OPTIMAL", "FEASIBLE"):
        action, why = "BLOCKED", "the planner could not produce a verified plan"
    elif gain_b0 > 0:
        action, why = (
            "RECOMMEND_PLAN",
            (f"the plan pays {_usd(gain_b0)} more invoice face on time than doing nothing"),
        )
    elif hints:
        action, why = (
            "NEGOTIATE",
            (
                "nothing more can be paid on time under current terms, but a bounded discount "
                "counteroffer could unlock an invoice"
            ),
        )
    elif bottlenecks:
        action, why = (
            "NO_ACTION",
            ("no feasible change pays more on time; the blocking reasons are listed"),
        )
    else:
        action, why = "NO_ACTION", "everything is already paid on time without coordination"

    flags: list[dict[str, Any]] = []
    for m in plan["economics"]["modeled_losses"]:
        flags.append(
            {
                "code": "PARTICIPANT_MODELED_LOSS",
                "merchant_id": m,
                "detail": f"modeled net {_usd(plan['merchant_benefits'][m]['net_benefit'])}",
            }
        )
    for leg in plan["selected_legs"]:
        if leg["fee_status"] != FeeStatus.KNOWN.value or "example" in leg["fee_source"]:
            who = leg["payee"] if leg["payee_fee_cents"] else leg["payer"]
            flags.append(
                {
                    "code": "FEE_UNCERTAIN",
                    "merchant_id": who,
                    "detail": f"{leg['leg_id']}: fee {leg['fee_status']} ({leg['fee_source']})",
                }
            )
    for p in net.positions:
        if p.blocked or p.unverified or p.held.cents or p.pending.cents:
            flags.append(
                {
                    "code": "FUNDS_UNAVAILABLE",
                    "merchant_id": p.merchant_id,
                    "detail": "has held, pending, unverified or currency-blocked funds "
                    "that count for zero",
                }
            )
    focal = net.focal_merchant
    alt = {a["name"]: a for a in plan["alternatives"]}
    if alt["focal_merchant"]["focal_net_cents"] > alt["network"]["focal_net_cents"]:
        flags.append(
            {
                "code": "BUYER_BELOW_OWN_BEST",
                "merchant_id": focal,
                "detail": "the buyer's own best plan earns it more than the network plan",
            }
        )
    if plan["required_approvals"]:
        flags.append(
            {
                "code": "APPROVALS_OUTSTANDING",
                "merchant_id": None,
                "detail": f"{len(plan['required_approvals'])} approvals required; none "
                "recorded (synthetic assumptions only)",
            }
        )
    if "DOWNSTREAM_NOT_PROVIDER_VERIFIED" in plan["warnings"]:
        flags.append(
            {
                "code": "DOWNSTREAM_SIMULATED",
                "merchant_id": None,
                "detail": "downstream legs are simulated, not provider-verified",
            }
        )

    return {
        "schema_version": OPPORTUNITY_SCHEMA,
        "status": "SIMULATED",
        "network_id": net.network_id,
        "as_of": net.as_of.isoformat(),
        "plan_id": plan["plan_id"],
        "input_digest": plan["input_digest"],
        "focal_merchant": focal,
        "recommendation": {"action": action, "reason": why},
        "comparison": {
            "on_time_face_cents": {k: _cents_str(v) for k, v in sorted(on_time.items())},
            "incremental_vs_B0": _cents_str(gain_b0),
            "incremental_vs_B1": _cents_str(on_time["B4"] - on_time["B1"]),
            "incremental_vs_B3": _cents_str(on_time["B4"] - on_time["B3"]),
            "simple_rule_matches_plan": on_time["B1"] >= on_time["B4"],
            "gross_flow_cents": plan["gross_flow_cents"],
            "note": "on-time face is original invoice face; gross flow counts re-spent cash",
        },
        "bottlenecks": bottlenecks,
        "plan_legs": [
            {
                k: leg[k]
                for k in (
                    "leg_id",
                    "obligation_id",
                    "payer",
                    "payee",
                    "amount_cents",
                    "concession_cents",
                    "slot_start",
                    "on_time",
                    "accelerated",
                    "status",
                    "depends_on",
                )
            }
            for leg in plan["selected_legs"]
        ],
        "negotiation_hints": hints,
        "merchant_benefits": plan["merchant_benefits"],
        "flags": flags,
        "execution": {"may_execute": False, "reason": "read-only simulation"},
        "_plan": plan,
    }


def merchant_view(report: dict[str, Any], merchant_id: str) -> dict[str, Any]:
    """What one merchant may see: its own legs, bottlenecks, benefit, hints and flags."""
    mine = [x for x in report["plan_legs"] if merchant_id in (x["payer"], x["payee"])]
    visible = {x["leg_id"] for x in mine}
    legs = []
    for x in mine:
        leg = {k: v for k, v in x.items() if k != "depends_on"}
        leg["depends_on_other_payments"] = bool(x["depends_on"])
        leg["depends_on"] = [d for d in x["depends_on"] if d in visible]
        legs.append(leg)
    own_b = [b for b in report["bottlenecks"] if merchant_id in (b["debtor"], b["creditor"])]
    return {
        "schema_version": report["schema_version"],
        "status": report["status"],
        "viewer": merchant_id,
        "plan_id": report["plan_id"],
        "recommendation": report["recommendation"],
        "my_actions": [
            {"action": "pay" if x["payer"] == merchant_id else "receive", **x} for x in legs
        ],
        "my_bottlenecks": own_b,
        "other_bottlenecks_count": len(report["bottlenecks"]) - len(own_b),
        "other_legs_count": len(report["plan_legs"]) - len(mine),
        "my_benefit": report["merchant_benefits"].get(merchant_id, {}),
        "my_negotiation_hints": [
            h for h in report["negotiation_hints"] if h["payer"] == merchant_id
        ],
        "my_flags": [f for f in report["flags"] if f["merchant_id"] in (merchant_id, None)],
        "comparison": (
            report["comparison"]
            if merchant_id == report["focal_merchant"]
            else {"note": "network comparison is shown to the initiating merchant only"}
        ),
        "execution": report["execution"],
    }


def facts(view: dict[str, Any]) -> list[tuple[str, str]]:
    out = [
        "This analysis is a SIMULATION; nothing has been paid, approved or changed.",
        f"Recommendation: {view['recommendation']['action']} - {view['recommendation']['reason']}.",
    ]
    for a in view["my_actions"]:
        out.append(
            f"You would {a['action']} {_usd(a['amount_cents'])} "
            f"{'to' if a['action'] == 'pay' else 'from'} "
            f"{a['payee'] if a['action'] == 'pay' else a['payer']} on {a['slot_start'][:10]} "
            f"for invoice {a['obligation_id']} ({'on time' if a['on_time'] else 'late'})."
        )
    for b in view["my_bottlenecks"]:
        out.append(
            f"Invoice {b['obligation_id']} ({_usd(b['face_cents'])}) is not paid on time without "
            f"coordination: {b['reason']}."
        )
    for h in view["my_negotiation_hints"]:
        if h["kind"] == "PENDING_PROPOSAL":
            out.append(h["detail"][0].upper() + h["detail"][1:] + ".")
            continue
        out.append(
            f"For invoice {h['obligation_id']}, the current offer is "
            f"{_usd(h['current_offer_cents'])} "
            f"off; a discount of at least {_usd(h['minimum_needed_cents'])} would fit your rules."
        )
    b = view["my_benefit"]
    if b and view["my_actions"]:
        out.append(f"Your modeled net benefit: {_usd(b['net_benefit'])}.")
    for f in view["my_flags"]:
        out.append(f"Flag {f['code']}: {f['detail']}.")
    c = view["comparison"]
    if "incremental_vs_B0" in c:
        out.append(
            f"Compared with doing nothing, {_usd(_num(c['incremental_vs_B0']))} more invoice face "
            f"is paid on time; compared with paying earliest-due-first, "
            f"{_usd(_num(c['incremental_vs_B1']))}."
        )
    return [(f"F{i}", s) for i, s in enumerate(out, start=1)]


def _num(s: str) -> Fraction:
    return Fraction(s.split(" ", 1)[0])


def focal_id(net: Network) -> str:
    return net.focal_merchant
