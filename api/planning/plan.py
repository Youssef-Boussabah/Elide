"""Deterministic, versioned plan record (`elide.plan.v1`) for the UI and later AI text.

`build_plan` runs the exact solver, checks its output with the independent verifier,
computes B0-B5 on the same network, alternatives, dependencies and binding constraints,
and returns a JSON-ready dict. Same input -> byte-identical record (no wall-clock times,
no solve timings). Every record has `status = "SIMULATED"` and
`execution.may_execute = false`. The planner may recommend; it never issues a PayPal
payment, alters an invoice or creates a merchant approval.

`merchant_view` gives one merchant only its own legs, approvals, benefit and reservation;
other merchants' cash, reserves, invoices and benefits are not included.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Any

from api.domain.money import USD, Money
from api.liquidity.capacity import Commitment
from api.planning.baselines import run_baselines
from api.planning.network import Network, TermsKind
from api.planning.objective import TIERS, Objective
from api.planning.solver import (
    SOLVER_NAME,
    SOLVER_VERSION,
    Funding,
    ScheduledLeg,
    SolveConfig,
    SolverStatus,
    minimum_liquidity,
    solve,
)
from api.planning.verifier import VERIFIER_VERSION, verify

PLAN_SCHEMA_VERSION = "elide.plan.v1"


def _digest(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _input_digest(net: Network, allow_proposals: bool, required: frozenset[str]) -> str:
    return _digest(
        {
            "network": net.digest(),
            "allow_proposals": allow_proposals,
            "required": sorted(required),
            "solver": f"{SOLVER_NAME}/{SOLVER_VERSION}",
        }
    )


def _frac(x: Fraction) -> str:
    return str(x)


def _dependencies(
    net: Network, sched: tuple[ScheduledLeg, ...], allow: bool
) -> dict[str, list[str]]:
    """Leg L depends on U when removing U makes L (or a later leg of L's payer) fail."""
    deps: dict[str, list[str]] = {x.option_id: [] for x in sched}
    for u in sched:
        rest = tuple(x for x in sched if x != u)
        rep = verify(net, rest, allow_proposals=allow)
        failing = {v.leg_id for v in rep.violations if v.code == "CAPACITY"}
        payee = net.option(u.option_id).payee
        for x in rest:
            if x.option_id in failing and net.option(x.option_id).payer == payee:
                deps[x.option_id].append(u.option_id)
    return {k: sorted(v) for k, v in deps.items()}


def _rejected(net: Network, sched: tuple[ScheduledLeg, ...], allow: bool) -> list[dict[str, str]]:
    chosen = {x.option_id for x in sched}
    paid = {net.option(x.option_id).obligation_id: x.option_id for x in sched}
    out = []
    for o in sorted(net.options, key=lambda o: o.option_id):
        if o.option_id in chosen:
            continue
        if o.terms is TermsKind.NEGOTIATED_PROPOSAL and not allow:
            out.append(
                {
                    "leg_id": o.option_id,
                    "code": "PROPOSAL_NOT_ENABLED",
                    "explanation": "negotiated change not assumed approved in this run (B4)",
                }
            )
            continue
        if o.obligation_id in paid:
            out.append(
                {
                    "leg_id": o.option_id,
                    "code": "OTHER_TERMS_SELECTED",
                    "explanation": f"invoice settled by {paid[o.obligation_id]}",
                }
            )
            continue
        first = None
        feasible = False
        for t in range(len(net.clock)):
            trial = (*sched, ScheduledLeg(o.option_id, t))
            rep = verify(net, trial, allow_proposals=allow)
            mine = [v for v in rep.violations if v.leg_id == o.option_id]
            if rep.ok:
                feasible = True
                break
            if first is None and mine:
                first = mine[0]
        if feasible:
            out.append(
                {
                    "leg_id": o.option_id,
                    "code": "NOT_SELECTED",
                    "explanation": "feasible, but the selected plan scores higher",
                }
            )
        elif first is not None:
            out.append({"leg_id": o.option_id, "code": first.code, "explanation": first.detail})
        else:
            out.append(
                {
                    "leg_id": o.option_id,
                    "code": "BREAKS_OTHER_LEGS",
                    "explanation": "adding it makes another selected leg infeasible",
                }
            )
    for x in net.exclusions:
        out.append({"leg_id": x.subject_id, "code": x.code, "explanation": x.explanation})
    return sorted(out, key=lambda r: (r["leg_id"], r["code"]))


def _metrics_summary(m) -> dict[str, int]:
    return {
        "on_time_face_cents": m.on_time_face,
        "gross_flow_cents": m.gross_flow,
        "focal_net_cents": m.focal_net,
        "surplus_cents": m.surplus,
        "fees_cents": m.fees,
    }


def build_plan(
    net: Network,
    *,
    allow_proposals: bool = False,
    required: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    cfg = SolveConfig(allow_proposals=allow_proposals, required=frozenset(required))
    res = solve(net, cfg)
    sched = res.schedule
    rep = verify(net, sched, allow_proposals=allow_proposals, required=cfg.required)
    verified = rep.ok or res.status is SolverStatus.INFEASIBLE
    m = rep.metrics
    focal = net.focal_merchant

    deps = _dependencies(net, sched, allow_proposals)
    reports = {r.leg_id: r for r in m.legs}
    selected = []
    approvals = []
    for x in sched:
        o = net.option(x.option_id)
        r = reports[x.option_id]
        leg_approvals = [
            {"merchant_id": a.merchant_id, "action": a.action, "basis": a.basis.value}
            for a in sorted(o.authorizations, key=lambda a: (a.merchant_id, a.action))
        ]
        for a in leg_approvals:
            approvals.append({"leg_id": o.option_id, **a})
        selected.append(
            {
                "leg_id": o.option_id,
                "obligation_id": o.obligation_id,
                "revision_no": o.revision_no,
                "terms": o.terms.value,
                "payer": o.payer,
                "payee": o.payee,
                "slot": x.slot,
                "slot_start": net.clock.start(x.slot).isoformat(),
                "amount_cents": o.cash.cents,
                "concession_cents": o.concession.cents,
                "extra_concession_cents": r.extra_concession_cents,
                "payer_fee_cents": o.fee.payer_fee.cents,
                "payee_fee_cents": o.fee.payee_fee.cents,
                "fee_status": o.fee.status.value,
                "fee_source": o.fee.source,
                "proceeds_cents": r.proceeds_cents,
                "usable_slot": r.usable_slot,
                "on_time": r.on_time,
                "accelerated": r.accelerated,
                "depends_on": deps[o.option_id],
                "status": "conditional" if deps[o.option_id] else "proposed",
                "approvals": leg_approvals,
            }
        )

    draws = [
        {
            "merchant_id": mo.merchant_id,
            "cents": mo.existing_draw_cents,
            "evidence_id": net.position(mo.merchant_id).evidence_id,
        }
        for mo in m.merchants
        if mo.existing_draw_cents
    ]
    benefits = {
        mo.merchant_id: {
            "discount_gained_cents": mo.discount_gained,
            "discount_given_cents": mo.discount_given,
            "fees_paid": mo.fees_paid,
            "opportunity_cost_cents": mo.opportunity_cost,
            "early_receipt_value_cents": mo.early_receipt_value,
            "net_benefit": mo.net_benefit,
            "opening_cents": mo.opening_cents,
            "closing_cents": mo.closing_cents,
        }
        for mo in m.merchants
    }

    baselines = run_baselines(net)
    plan_face = Fraction(m.on_time_face)
    incremental = {
        f"vs_{b.baseline_id}": _frac(plan_face - b.expected_on_time_face) for b in baselines
    }

    alts = []
    focal_res = solve(net, SolveConfig(objective=Objective.FOCAL, allow_proposals=allow_proposals))
    for name, s in (("network", sched), ("focal_merchant", focal_res.schedule), ("no_action", ())):
        am = verify(net, s, allow_proposals=allow_proposals).metrics
        alts.append(
            {"name": name, "legs": [f"{x.option_id}@{x.slot}" for x in s], **_metrics_summary(am)}
        )
    for a in alts:
        a["pareto"] = not any(
            b is not a
            and b["on_time_face_cents"] >= a["on_time_face_cents"]
            and b["focal_net_cents"] >= a["focal_net_cents"]
            and b["surplus_cents"] >= a["surplus_cents"]
            and (b["on_time_face_cents"], b["focal_net_cents"], b["surplus_cents"])
            != (a["on_time_face_cents"], a["focal_net_cents"], a["surplus_cents"])
            for b in alts
        )

    binding = []
    for mo in m.merchants:
        if mo.min_slack_cents == 0:
            binding.append(
                {
                    "kind": "RESERVE",
                    "merchant_id": mo.merchant_id,
                    "slot": mo.min_slack_slot,
                    "detail": "cash after the leg equals reserve + commitments exactly",
                }
            )
    deadlines = []
    for x in sched:
        o = net.option(x.option_id)
        if o.expires_at is not None:
            deadlines.append(o.expires_at)
            if net.clock.last_slot_before(o.expires_at) == x.slot:
                binding.append(
                    {
                        "kind": "OFFER_EXPIRY",
                        "leg_id": o.option_id,
                        "slot": x.slot,
                        "detail": f"last slot before the offer deadline {o.expires_at.isoformat()}",
                    }
                )
        valid = net.position(o.payer).valid_until
        if valid is not None:
            deadlines.append(valid)
        due = net.clock.last_slot_at_or_before(net.obligation(o.obligation_id).due_at)
        if due == x.slot:
            binding.append(
                {
                    "kind": "DEADLINE",
                    "leg_id": o.option_id,
                    "slot": x.slot,
                    "detail": "paid in its due slot",
                }
            )
    expires = min(deadlines) if deadlines else net.clock.start(len(net.clock) - 1)

    reasons: list[str] = []
    min_liq = None
    if res.status is SolverStatus.INFEASIBLE and cfg.required:
        funding = tuple(Funding(mm, hypothetical_allowed=True) for mm in net.merchants)
        ml = minimum_liquidity(net, cfg.required, funding, allow_proposals=allow_proposals)
        if ml.status is SolverStatus.OPTIMAL:
            probe = verify(net, ml.schedule, allow_proposals=allow_proposals, required=cfg.required)
            short = sorted({v.merchant for v in probe.violations if v.code == "CAPACITY"})
            reasons.extend(v.detail for v in probe.violations if v.code == "CAPACITY")
            for mm in short:
                if mm is None:
                    continue
                pos = net.position(mm)
                for b in pos.blocked:
                    reasons.append(
                        f"{mm}: {b} is currency-blocked; paying USD would need a separately "
                        "authorized conversion (D008)"
                    )
                for u in pos.unverified:
                    reasons.append(f"{mm}: {u.amount} counts for zero ({u.reason})")
            min_liq = {
                "label": "HYPOTHETICAL external funding that does not exist; sizes the gap only",
                "status": ml.status.value,
                "schedule": [f"{x.option_id}@{x.slot}" for x in ml.schedule],
                "existing_cents": dict(sorted(ml.existing.items())),
                "hypothetical_cents": dict(sorted(ml.hypothetical.items())),
                "notes": list(ml.notes),
            }
        for oid in sorted(cfg.required):
            if not net.options_for(oid):
                why = [x.explanation for x in net.exclusions if x.obligation_id == oid]
                reasons.append(f"{oid}: no executable leg ({'; '.join(why) or 'none offered'})")
        if not reasons:
            reasons.append("no schedule meets every required obligation by its due slot")

    warnings = list(net.labels)
    if any(x["payer"] != focal for x in selected):
        warnings.append("DOWNSTREAM_NOT_PROVIDER_VERIFIED")
    if any(x["terms"] == TermsKind.NEGOTIATED_PROPOSAL.value for x in selected):
        warnings.append("NEGOTIATED_CHANGE_ASSUMED_APPROVED")
    if approvals:
        warnings.append("APPROVALS_ARE_SYNTHETIC_ASSUMPTIONS")
    if not verified:
        warnings.append("REJECTED_BY_VERIFIER")
    warnings.append("SIMULATED_NOT_EXECUTABLE")

    assumptions = [
        f"availability model {net.availability.name}: proceeds usable "
        f"{net.availability.default_lag} slot(s) after payment (never the same slot)",
        "cash, reserves and commitments from each merchant's position at slot 0",
    ]
    for mm in net.merchants:
        pos = net.position(mm)
        assumptions.append(
            f"{mm}: {pos.evidence_kind} evidence {pos.evidence_id}"
            + (f", valid until {pos.valid_until.isoformat()}" if pos.valid_until else "")
        )
    fee_sources = sorted({x["fee_source"] for x in selected})
    assumptions.append(f"fee sources: {', '.join(fee_sources) or 'none'}")

    record: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "status": "SIMULATED",
        "input_digest": _input_digest(net, allow_proposals, cfg.required),
        "network_id": net.network_id,
        "focal_merchant": focal,
        "currency": USD,
        "as_of": net.as_of.isoformat(),
        "expires_at": expires.isoformat(),
        "config": {"allow_proposals": allow_proposals, "required": sorted(cfg.required)},
        "solver": {
            "name": SOLVER_NAME,
            "version": SOLVER_VERSION,
            "status": res.status.value,
            "complete": res.complete,
            "nodes": res.nodes,
            "objective": Objective.NETWORK.value,
            "tiers": list(TIERS),
            "value": list(res.value) if res.value is not None else None,
            "note": res.note,
        },
        "verifier": {
            "version": VERIFIER_VERSION,
            "ok": rep.ok,
            "violations": [f"{v.code}: {v.detail}" for v in rep.violations],
        },
        "selected_legs": selected,
        "rejected_legs": _rejected(net, sched, allow_proposals),
        "required_initial_capacity": draws,
        "merchant_benefits": benefits,
        "fees_cents": m.fees,
        "gross_flow_cents": m.gross_flow,
        "on_time_face_cents": m.on_time_face,
        "downstream_face_cents": m.downstream_face,
        "liquidity_dollar_days": str(m.liquidity_dollar_days),
        "early_cash_cents": m.early_cash,
        "availability_assumptions": assumptions,
        "baselines": [
            {
                "baseline_id": b.baseline_id,
                "name": b.name,
                "expected_on_time_face_cents": _frac(b.expected_on_time_face),
                "expected_gross_flow_cents": _frac(b.expected_gross_flow),
                "expected_focal_net_cents": _frac(b.expected_focal_net),
                "outcomes_considered": b.outcomes_considered,
                "schedule": [f"{x.option_id}@{x.slot}" for x in b.schedule],
                "notes": list(b.notes),
            }
            for b in baselines
        ],
        "incremental_on_time_face_cents": incremental,
        "binding_constraints": binding,
        "infeasibility_reasons": reasons,
        "required_approvals": sorted(
            approvals, key=lambda a: (a["leg_id"], a["merchant_id"], a["action"])
        ),
        "alternatives": alts,
        "minimum_liquidity": min_liq,
        "warnings": warnings,
        "execution": {
            "may_execute": False,
            "reason": "SIMULATED: synthetic network and assumed approvals. The planner never "
            "issues a payment, alters an invoice or records a merchant approval.",
        },
    }
    record["plan_id"] = "plan_" + _digest(record)[:16]
    return record


def plan_commitments(record: dict[str, Any]) -> tuple[Commitment, ...]:
    """Cash reservations a plan would need from merchants' existing verified funds."""
    return tuple(
        Commitment(
            f"{record['plan_id']}:{d['merchant_id']}",
            d["merchant_id"],
            record["plan_id"],
            Money(d["cents"], USD),
        )
        for d in record["required_initial_capacity"]
    )


def merchant_view(record: dict[str, Any], merchant_id: str) -> dict[str, Any]:
    mine = [x for x in record["selected_legs"] if merchant_id in (x["payer"], x["payee"])]
    visible = {x["leg_id"] for x in mine}
    legs = []
    for x in mine:
        leg = {k: v for k, v in x.items() if k not in ("approvals", "depends_on")}
        leg["depends_on"] = [d for d in x["depends_on"] if d in visible]
        leg["has_upstream_dependency"] = any(d not in visible for d in x["depends_on"])
        leg["my_approvals"] = [a for a in x["approvals"] if a["merchant_id"] == merchant_id]
        legs.append(leg)
    return {
        "schema_version": record["schema_version"],
        "plan_id": record["plan_id"],
        "status": record["status"],
        "viewer": merchant_id,
        "expires_at": record["expires_at"],
        "solver_status": record["solver"]["status"],
        "my_legs": legs,
        "my_approvals": [
            a for a in record["required_approvals"] if a["merchant_id"] == merchant_id
        ],
        "my_benefit": record["merchant_benefits"].get(merchant_id, {}),
        "my_reservation": [
            d for d in record["required_initial_capacity"] if d["merchant_id"] == merchant_id
        ],
        "downstream_legs_count": len(record["selected_legs"]) - len(mine),
        "execution": record["execution"],
    }


def verify_plan_record(net: Network, record: dict[str, Any]) -> list[str]:
    """Re-check a stored plan record against the network with the verifier."""
    problems: list[str] = []
    allow = bool(record["config"]["allow_proposals"])
    required = frozenset(record["config"]["required"])
    if record.get("status") != "SIMULATED" or record["execution"]["may_execute"] is not False:
        problems.append("a P2 plan must be SIMULATED and not executable")
    if record["input_digest"] != _input_digest(net, allow, required):
        problems.append("input digest does not match this network")
    legs = [x for x in record["selected_legs"] if net.has_option(x["leg_id"])]
    if len(legs) != len(record["selected_legs"]):
        problems.append("unknown leg in record")
    sched = tuple(ScheduledLeg(x["leg_id"], int(x["slot"])) for x in legs)
    rep = verify(net, sched, allow_proposals=allow, required=required)
    if record["solver"]["status"] != SolverStatus.INFEASIBLE.value:
        problems.extend(f"{v.code}: {v.detail}" for v in rep.violations)
    reports = {r.leg_id: r for r in rep.metrics.legs}
    for x in legs:
        o = net.option(x["leg_id"])
        r = reports.get(x["leg_id"])
        expected = {
            "amount_cents": o.cash.cents,
            "concession_cents": o.concession.cents,
            "payer_fee_cents": o.fee.payer_fee.cents,
            "payee_fee_cents": o.fee.payee_fee.cents,
            "payer": o.payer,
            "payee": o.payee,
            "obligation_id": o.obligation_id,
            "revision_no": o.revision_no,
        }
        for k, v in expected.items():
            if x.get(k) != v:
                problems.append(f"{x['leg_id']}: {k} is {x.get(k)!r}, network says {v!r}")
        if r is not None and x.get("usable_slot") != r.usable_slot:
            problems.append(f"{x['leg_id']}: usable_slot mismatch")
        slot_of = {y["leg_id"]: int(y["slot"]) for y in legs}
        for d in x.get("depends_on", []):
            ur = reports.get(d)
            if ur is None or ur.usable_slot is None or ur.usable_slot > int(x["slot"]):
                problems.append(f"{x['leg_id']}: depends on {d}, whose proceeds are not usable yet")
            elif d not in slot_of:
                problems.append(f"{x['leg_id']}: depends on unscheduled {d}")
    draws = sorted(
        (mo.merchant_id, mo.existing_draw_cents)
        for mo in rep.metrics.merchants
        if mo.existing_draw_cents
    )
    declared = sorted((d["merchant_id"], d["cents"]) for d in record["required_initial_capacity"])
    if draws != declared:
        problems.append(f"declared initial capacity {declared} != replayed {draws}")
    if record["gross_flow_cents"] != rep.metrics.gross_flow:
        problems.append("gross flow mismatch")
    if record["on_time_face_cents"] != rep.metrics.on_time_face:
        problems.append("on-time face mismatch")
    return problems
