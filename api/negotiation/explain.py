"""Plain-language explanation of a deterministic P2 plan for ONE merchant.

The plan record is first re-verified against the current network (`verify_plan_record`);
a stale or altered plan is refused before any model call. Facts are built only from that
merchant's `merchant_view` (plus infeasibility reasons about that merchant itself), so
the model never sees another merchant's private data. The model's text must cite only
amounts present in the facts and name no merchant outside the view; otherwise the
deterministic template is used instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api.ai.adapter import LLMAdapter, LLMOutputError, LLMUnavailableError, ModelCall
from api.ai.prompts import EXPLAIN_PROMPT_VERSION, EXPLAIN_SYSTEM
from api.ai.schemas import PlanExplanation
from api.negotiation.guards import TextIssue, all_numbers, check_text
from api.planning.network import Network
from api.planning.plan import merchant_view, verify_plan_record


def _usd(cents: int) -> str:
    return f"USD {cents // 100:,}.{cents % 100:02d}"


@dataclass
class ExplanationResult:
    status: str  # EXPLAINED / TEMPLATE / STALE_OR_TAMPERED
    viewer: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""
    source: str = "deterministic_template"
    model_call: ModelCall | None = None
    issues: list[TextIssue] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def plan_facts(record: dict[str, Any], viewer: str) -> list[tuple[str, str]]:
    view = merchant_view(record, viewer)
    facts: list[str] = [
        f"Plan {view['plan_id']} is a SIMULATION (status {view['status']}); nothing has "
        "been paid or approved.",
        f"Solver status: {view['solver_status']}. The plan expires at {view['expires_at']}.",
    ]
    for leg in view["my_legs"]:
        role = "you pay" if leg["payer"] == viewer else "you receive"
        other = leg["payee"] if leg["payer"] == viewer else leg["payer"]
        facts.append(
            f"Leg {leg['leg_id']}: {role} {_usd(leg['amount_cents'])} "
            f"({'with' if leg['concession_cents'] else 'no'} discount "
            f"{_usd(leg['concession_cents'])}) {'to' if role == 'you pay' else 'from'} {other} "
            f"on {leg['slot_start'][:10]}; {'on time' if leg['on_time'] else 'late'}; "
            f"status {leg['status']}"
            + (
                "; depends on an earlier payment to you"
                if leg["has_upstream_dependency"] or leg["depends_on"]
                else ""
            )
        )
    b = view["my_benefit"]
    if b:
        net = b["net_benefit"]
        net_s = _usd(net) if net >= 0 else "-" + _usd(-net)
        facts.append(
            f"Your modeled net benefit: {net_s} (discount gained "
            f"{_usd(b['discount_gained_cents'])}, discount given "
            f"{_usd(b['discount_given_cents'])}, cost of paying early "
            f"{_usd(b['opportunity_cost_cents'])})."
        )
    for r in view["my_reservation"]:
        facts.append(f"Cash you would reserve from verified funds: {_usd(r['cents'])}.")
    if view["my_approvals"]:
        acts = sorted({a["action"].replace("_", " ") for a in view["my_approvals"]})
        facts.append(f"Your approvals still required: {', '.join(acts)}.")
    if view["downstream_legs_count"] and viewer == record["focal_merchant"]:
        facts.append(
            f"{view['downstream_legs_count']} other payment(s) in the plan do not involve you."
        )
    own_reason = False
    for reason in record.get("infeasibility_reasons", []):
        if reason.startswith(viewer + " ") or reason.startswith(viewer + ":"):
            facts.append(reason.replace(viewer, "You", 1))
            own_reason = True
    if record.get("infeasibility_reasons") and not own_reason:
        facts.append("A required payment is not feasible for reasons private to another merchant.")
    return [(f"F{i}", f) for i, f in enumerate(facts, start=1)]


def narrate(
    adapter: LLMAdapter | None,
    facts: list[tuple[str, str]],
    viewer: str,
    forbidden_names: tuple[str, ...],
) -> ExplanationResult:
    """Model-written text from verified facts, checked; the template is the fallback."""
    template = " ".join(f for _, f in facts)
    result = ExplanationResult("TEMPLATE", viewer, facts=facts, text=template)
    if adapter is None:
        return result
    user = "<facts>\n" + "\n".join(f"{k}: {v}" for k, v in facts) + "\n</facts>"
    try:
        out = adapter.generate_structured(
            system=EXPLAIN_SYSTEM,
            user=user,
            schema=PlanExplanation,
            schema_name="plan_explanation",
            prompt_version=EXPLAIN_PROMPT_VERSION,
            max_tokens=700,
        )
    except (LLMUnavailableError, LLMOutputError) as exc:
        result.issues.append(TextIssue("MODEL_FAILED", str(exc)))
        return result
    text = f"{out.value.headline}\n{out.value.explanation}"
    issues = check_text(
        text,
        allowed_amounts=all_numbers(template),
        private_amounts=set(),
        forbidden_names=forbidden_names,
    )
    bad_cites = [c for c in out.value.cited_facts if c not in {k for k, _ in facts}]
    if bad_cites:
        issues.append(TextIssue("UNKNOWN_FACT_CITED", ", ".join(bad_cites)))
    result.model_call = out.call
    if issues:
        result.issues = issues
        return result  # deterministic template stays
    result.status, result.text, result.source = "EXPLAINED", text, "model"
    return result


def explain_plan(
    adapter: LLMAdapter | None, net: Network, record: dict[str, Any], viewer: str
) -> ExplanationResult:
    problems = verify_plan_record(net, record)
    if problems:
        return ExplanationResult(
            "STALE_OR_TAMPERED",
            viewer,
            problems=problems[:5],
            text="This plan no longer matches the current data; recompute it.",
        )
    view = merchant_view(record, viewer)
    visible = {viewer} | {x for leg in view["my_legs"] for x in (leg["payer"], leg["payee"])}
    everyone = {x for leg in record["selected_legs"] for x in (leg["payer"], leg["payee"])}
    others = tuple(sorted((everyone | {record["focal_merchant"]}) - visible))
    return narrate(adapter, plan_facts(record, viewer), viewer, others)
