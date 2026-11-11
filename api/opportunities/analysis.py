"""Proactive, read-only opportunity analysis over a P2 network (SYNTHETIC scope).

Starting only from the network and its financial evidence, this module:
- runs the verified P2 plan (B4) and the B0-B5 baselines;
- lists bottlenecks: invoices not paid on time without coordination (B0), with the
  deterministic reason (capacity, the payer's or payee's own rules, expired offer,
  currency-blocked or unverified/held funds, dispute) and whether the plan unlocks them;
- separates the decision into feasibility, advantage over baselines (B0 do nothing, B1
  earliest-due-first, B3 random buyer tie-break), each participant's modeled net benefit,
  fee/availability uncertainty and outstanding consent, then recommends one of:
  RECOMMEND_PLAN (feasible, pays more on time than doing nothing, and no participant has
  a modeled loss it has not declared it accepts), REVIEW_ECONOMICS (pays more on time,
  but some participant has an undeclared modeled loss), NEGOTIATE (no improvement today,
  but a bounded discount counteroffer could unlock an invoice), NO_ACTION (nothing to
  gain; the honest default) or BLOCKED (no verified plan). Every recommendation is
  conditional: no approval is recorded and no downstream funds are verified spendable;
- reports increments against B0 (do nothing), B1 (earliest-due-first) and B3 (random
  buyer tie-break), and says plainly when a simple rule already matches the plan;
- flags supplier modeled losses, uncertain fees, unavailable or blocked funds, the buyer
  earning less than its own best choice, synthetic approvals that are still outstanding,
  and simulated downstream legs.

`merchant_view` derives a separate view per merchant: its own legs, its own invoices (the
blocking reason only when it is the payer), its own benefit, approvals and flags, and a
personal recommendation. Network totals, counts and comparisons go to the initiating
merchant only. `facts` turns that view into numbered facts for the explanation layer.
Nothing here changes an invoice, a reserve, a commitment or an approval.
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

    gain_b0 = on_time["B4"] - on_time["B0"]
    gain_b1 = on_time["B4"] - on_time["B1"]
    gain_b3 = on_time["B4"] - on_time["B3"]
    feasible = plan["solver"]["status"] in ("OPTIMAL", "FEASIBLE") and plan["verifier"]["ok"]
    participants = plan["economics"]["participants"]
    nets = {m: plan["merchant_benefits"][m]["net_benefit"] for m in participants}
    unjustified = []
    for m in participants:
        floor = net.policy(m).min_receipt_net_benefit_cents
        if nets[m] < 0 and (floor is None or nets[m] < floor):
            unjustified.append(m)  # a loss the merchant has not declared it accepts
    uncertainty = sorted(
        {
            f["code"]
            for f in flags
            if f["code"] in ("FEE_UNCERTAIN", "FUNDS_UNAVAILABLE", "DOWNSTREAM_SIMULATED")
        }
    )
    approvals_needed = len(plan["required_approvals"])
    decision = {
        "feasible": feasible,
        "advantage": {
            "vs_do_nothing_B0": _cents_str(gain_b0),
            "vs_earliest_due_first_B1": _cents_str(gain_b1),
            "vs_random_tie_break_B3": _cents_str(gain_b3),
            "network_awareness_needed": gain_b1 > 0,
        },
        "buyer_net_benefit_cents": nets.get(focal, 0) if focal in participants else None,
        "participant_net_benefit_cents": nets,
        "unjustified_modeled_losses": unjustified,
        "uncertainty": uncertainty,
        "consent": {"approvals_required": approvals_needed, "approvals_recorded": 0},
    }
    conditions = [
        "every payer and counterparty must approve its own leg; no approval is recorded",
        "downstream receipts are simulated; no downstream funds are verified spendable",
    ]
    if "FEE_UNCERTAIN" in uncertainty:
        conditions.append("at least one fee is a ceiling, an estimate or an example")
    if "FUNDS_UNAVAILABLE" in uncertainty:
        conditions.append("some funds are held, unverified or currency-blocked and count for zero")
    same = (
        " Paying earliest-due-first already achieves the same on-time result, so this is not "
        "a network-aware improvement."
        if gain_b0 > 0 and gain_b1 <= 0
        else ""
    )
    if not feasible:
        action, why = "BLOCKED", "the planner could not produce a verified plan"
    elif gain_b0 > 0 and unjustified:
        action, why = (
            "REVIEW_ECONOMICS",
            f"the plan pays {_usd(gain_b0)} more invoice face on time than doing nothing, but "
            f"{len(unjustified)} participant(s) have a modeled loss with no declared offsetting "
            "benefit; review the economics before asking anyone to approve." + same,
        )
    elif gain_b0 > 0:
        action, why = (
            "RECOMMEND_PLAN",
            f"the plan pays {_usd(gain_b0)} more invoice face on time than doing nothing and no "
            "participant has an undeclared modeled loss; it is a candidate for each merchant's "
            "own approval." + same,
        )
    elif hints:
        action, why = (
            "NEGOTIATE",
            "nothing more can be paid on time under current terms, but a bounded discount "
            "counteroffer could unlock an invoice",
        )
    elif bottlenecks:
        action, why = (
            "NO_ACTION",
            "no feasible change pays more on time; the blocking reasons are listed",
        )
    else:
        action, why = "NO_ACTION", "everything is already paid on time without coordination"

    return {
        "schema_version": OPPORTUNITY_SCHEMA,
        "status": "SIMULATED",
        "network_id": net.network_id,
        "as_of": net.as_of.isoformat(),
        "plan_id": plan["plan_id"],
        "input_digest": plan["input_digest"],
        "focal_merchant": focal,
        "recommendation": {"action": action, "reason": why},
        "decision": decision,
        "conditions": conditions,
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
        "execution": {
            "may_execute": False,
            "reason": "read-only simulation: no payment authorization or spendable downstream "
            "funds exist",
        },
        "_plan": plan,
    }


def _personal_recommendation(report: dict[str, Any], me: str, legs, benefit) -> dict[str, Any]:
    """A recommendation derived for one merchant from its own data only."""
    action = report["recommendation"]["action"]
    if me == report["focal_merchant"]:
        return report["recommendation"]
    if not legs:
        return {
            "action": "NOTHING_FOR_YOU",
            "reason": "you have no payment to make or receive in this plan",
        }
    net = benefit.get("net_benefit", 0)
    pays = [x for x in legs if x["payer"] == me]
    gets = [x for x in legs if x["payee"] == me]
    parts = []
    if gets:
        parts.append(f"receive {len(gets)} payment(s)")
    if pays:
        parts.append(f"make {len(pays)} payment(s)")
    base = (
        f"you would {' and '.join(parts)} in this simulated plan; your modeled net benefit is "
        f"{_usd(net)}; nothing happens without your own approval"
    )
    if net < 0 and me in report["decision"]["unjustified_modeled_losses"]:
        return {
            "action": "REVIEW_ECONOMICS",
            "reason": base + "; you have not declared a benefit that offsets this loss",
        }
    if action in ("RECOMMEND_PLAN", "REVIEW_ECONOMICS"):
        return {"action": "REVIEW_AND_DECIDE", "reason": base}
    return {"action": action, "reason": base}


def merchant_view(report: dict[str, Any], merchant_id: str) -> dict[str, Any]:
    """What one merchant may see, derived separately from the network report.

    Every merchant: its own legs, its own invoices' status, its own benefit, its own
    approvals and flags. Only the payer of an invoice sees why that payment is blocked
    (the reason reflects the payer's private cash and rules). Network totals, counts and
    baseline comparisons go to the initiating (focal) merchant only.
    """
    me = merchant_id
    focal = me == report["focal_merchant"]
    mine = [x for x in report["plan_legs"] if me in (x["payer"], x["payee"])]
    visible = {x["leg_id"] for x in mine}
    legs = []
    for x in mine:
        leg = {k: v for k, v in x.items() if k != "depends_on"}
        leg["depends_on_other_payments"] = bool(x["depends_on"])
        leg["depends_on"] = [d for d in x["depends_on"] if d in visible]
        legs.append(leg)
    own_b = []
    for b in report["bottlenecks"]:
        if me == b["debtor"]:
            own_b.append(b)
        elif me == b["creditor"]:
            own_b.append(
                {
                    **{
                        k: b[k]
                        for k in (
                            "obligation_id",
                            "debtor",
                            "creditor",
                            "face_cents",
                            "open_cents",
                            "due_at",
                            "unlocked_by_plan",
                        )
                    },
                    "without_coordination": "not paid on time",
                    "reason_code": "UNLOCKED_BY_PLAN" if b["unlocked_by_plan"] else "PAYER_SIDE",
                    "reason": "the recommended plan pays it on time"
                    if b["unlocked_by_plan"]
                    else "the payer cannot pay it on time; the reason is private to the payer",
                }
            )
    benefit = report["merchant_benefits"].get(me, {}) if mine else {}
    my_approvals = sum(1 for a in report["_plan"]["required_approvals"] if a["merchant_id"] == me)
    my_flags = [f for f in report["flags"] if f["merchant_id"] == me]
    if focal:
        my_flags += [f for f in report["flags"] if f["merchant_id"] is None]
    elif mine:
        my_flags.append(
            {
                "code": "MY_APPROVALS_OUTSTANDING",
                "merchant_id": me,
                "detail": f"{my_approvals} of your approvals would be required; none recorded",
            }
        )
        my_flags.append(
            {
                "code": "SIMULATED_PLAN",
                "merchant_id": me,
                "detail": "payments and receipts in this plan are simulated; nothing is "
                "verified spendable",
            }
        )
    view = {
        "schema_version": report["schema_version"],
        "status": report["status"],
        "viewer": me,
        "plan_id": report["plan_id"],
        "recommendation": _personal_recommendation(report, me, legs, benefit),
        "my_actions": [{"action": "pay" if x["payer"] == me else "receive", **x} for x in legs],
        "my_bottlenecks": own_b,
        "my_benefit": benefit,
        "my_negotiation_hints": [h for h in report["negotiation_hints"] if h["payer"] == me],
        "my_flags": my_flags,
        "execution": report["execution"],
    }
    if focal:
        d = report["decision"]
        view["decision"] = {  # other participants' nets reflect their private inputs
            "feasible": d["feasible"],
            "advantage": d["advantage"],
            "buyer_net_benefit_cents": d["buyer_net_benefit_cents"],
            "participants_with_undeclared_losses": len(d["unjustified_modeled_losses"]),
            "uncertainty": d["uncertainty"],
            "consent": d["consent"],
        }
        view["conditions"] = report["conditions"]
        view["comparison"] = report["comparison"]
        view["other_bottlenecks_count"] = len(report["bottlenecks"]) - len(own_b)
        view["other_legs_count"] = len(report["plan_legs"]) - len(mine)
    else:
        view["conditions"] = [report["conditions"][0]] if mine else []
    return view


def facts(view: dict[str, Any]) -> list[tuple[str, str]]:
    out = [
        "This analysis is a SIMULATION; nothing has been paid, approved or changed, and no "
        "approval or spendable downstream funds exist yet.",
        f"Recommendation for you: {view['recommendation']['action']} - "
        f"{view['recommendation']['reason']}.",
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
    for c in view.get("conditions", []):
        out.append(f"Condition: {c}.")
    c = view.get("comparison")
    if c:
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
