"""P3 AI evaluation on the labelled set `policy-eval-v1`, plus one counteroffer and one
plan explanation.

    uv run python scripts/run_ai_eval.py --mode live   # real model from .env (costs money)
    uv run python scripts/run_ai_eval.py --mode mock   # reference extractions, no model

Model outputs (REAL or MOCK) are reported separately from deterministic verdicts.
Metrics: schema validity, status agreement with labels, exact extraction of labelled
values, abstention on unclear/unsafe cases, and unsafe passes (labelled not-ready but
validated READY: must be 0). Live mode refuses to start if the spend cap is reached.
Writes evidence/phase3/ai_eval_<mode>_<UTC date>.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.ai.adapter import UnconfiguredLLMAdapter, get_llm_adapter  # noqa: E402
from api.ai.mock import ScriptedLLMAdapter  # noqa: E402
from api.ai.prompts import (  # noqa: E402
    COUNTEROFFER_PROMPT_VERSION,
    EXPLAIN_PROMPT_VERSION,
    POLICY_PROMPT_VERSION,
)
from api.ai.spend import SpendGuard  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.negotiation.counteroffer import propose_counteroffer  # noqa: E402
from api.negotiation.explain import explain_plan, narrate  # noqa: E402
from api.opportunities.analysis import analyze, facts, merchant_view  # noqa: E402
from api.planning.plan import build_plan  # noqa: E402
from api.policy.service import draft_policy  # noqa: E402
from fixtures import ai_demo  # noqa: E402
from fixtures import planning as fx  # noqa: E402
from fixtures.ai_eval_cases import CASES, EVAL_VERSION, READY  # noqa: E402


def values_match(proposed: dict, expected: dict) -> bool:
    for key, want in expected.items():
        if key == "valid_until_date":
            if not (proposed.get("valid_until") or "").startswith(want):
                return False
        elif key == "offer_deadline_date":
            if not (proposed.get("offer_response_deadline") or "").startswith(want):
                return False
        elif key == "prohibited":
            if list(proposed.get("prohibited") or []) != want:
                return False
        elif proposed.get(key) != want:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "mock"], required=True)
    ap.add_argument("--model", default=None, help="override LLM_MODEL for this run")
    args = ap.parse_args()
    if args.mode == "live":
        settings = get_settings()
        if args.model:
            settings = settings.model_copy(update={"llm_model": args.model})
        adapter = get_llm_adapter(settings)
        if isinstance(adapter, UnconfiguredLLMAdapter):
            print("no LLM configured in .env")
            return 2
        spent = SpendGuard(get_settings().llm_spend_cap_usd).total()
        print(f"model {adapter.model}; spent so far ~USD {spent}")
    else:
        by_text = {c["text"]: c["reference"] for c in CASES}

        def responder(name, system, user):
            if name == "policy_extraction":
                text = user.split("<instruction>\n", 1)[1].rsplit("\n</instruction>", 1)[0]
                return by_text[text]
            if name == "counteroffer":
                return {
                    "invoice_id": "food_001",
                    "proposed_concession": "10.00",
                    "currency": "USD",
                    "message_to_counterparty": "Could we settle invoice food_001 ($1,000.00) "
                    "today for $990.00 with a $10.00 early-payment discount?",
                    "rationale_for_requester": "The $5 offer is below your minimum.",
                    "reason_codes": ["OFFER_BELOW_REQUESTER_MINIMUM"],
                }
            return {
                "headline": "Plan",
                "explanation": "This is a simulation; nothing was paid.",
                "cited_facts": ["F1"],
            }

        adapter = ScriptedLLMAdapter(responder, model="reference-extractions")

    rows = []
    for c in CASES:
        d = draft_policy(
            adapter,
            merchant_id="buyer",
            text=c["text"],
            source=c["source"],
            as_of=ai_demo.AS_OF,
            context=ai_demo.merchant_context("buyer"),
        )
        status = d.status
        expected = c["expected_status"]
        proposed = None
        if d.report and d.report.proposed:
            from dataclasses import asdict

            proposed = asdict(d.report.proposed)
        rows.append(
            {
                "id": c["id"],
                "category": c["category"],
                "expected": expected,
                "status": status,
                "schema_valid": d.outcome == "VALIDATED",
                "status_ok": status in expected,
                "values_ok": (
                    values_match(proposed or {}, c["expected_values"])
                    if READY in expected and status == READY
                    else None
                ),
                "unsafe_pass": READY not in expected and status == READY,
                "issues": [i.code for i in d.report.issues] if d.report else [],
                "model_output": d.extraction.model_dump() if d.extraction else None,
                "model_call": d.model_call.as_dict() if d.model_call else None,
            }
        )
        print(f"{c['id']:8} {status:24} {'ok' if rows[-1]['status_ok'] else 'MISMATCH'}")

    co = propose_counteroffer(adapter, ai_demo.negotiation_case())
    net = fx.network_two_candidates()
    ex = explain_plan(adapter, net, build_plan(net), "buyer")
    neg = fx.network_negotiation()
    opp_view = merchant_view(analyze(neg), "buyer")
    others = tuple(sorted(set(neg.merchants) - {"buyer", "food_supplier"}))
    opp = narrate(adapter, facts(opp_view), "buyer", others)

    n = len(rows)
    ready_cases = [r for r in rows if READY in r["expected"]]
    not_ready = [r for r in rows if READY not in r["expected"]]
    calls = [r["model_call"] for r in rows if r["model_call"]]
    calls += [x.as_dict() for x in (co.model_call, ex.model_call, opp.model_call) if x]
    summary = {
        "cases": n,
        "schema_valid": sum(r["schema_valid"] for r in rows),
        "schema_failures": sum(1 for r in rows if r["status"] == "MODEL_OUTPUT_INVALID"),
        "provider_call_failures": sum(1 for r in rows if r["status"] == "MODEL_UNAVAILABLE"),
        "status_agreement": sum(r["status_ok"] for r in rows),
        "ready_cases": len(ready_cases),
        "ready_correct_values": sum(1 for r in ready_cases if r["values_ok"]),
        "not_ready_cases": len(not_ready),
        "abstained_correctly": sum(1 for r in not_ready if r["status"] != READY),
        "false_abstentions": sum(1 for r in ready_cases if r["status"] != READY),
        "unsafe_passes": sum(r["unsafe_pass"] for r in rows),
        "input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
        "output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
        "estimated_cost_usd": str(sum(float(c.get("estimated_cost_usd") or 0) for c in calls)),
    }
    doc = {
        "evidence_class": "REAL model inference + deterministic validation"
        if args.mode == "live"
        else "MOCK (reference extractions) + deterministic validation",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "eval_version": EVAL_VERSION,
        "prompt_versions": [
            POLICY_PROMPT_VERSION,
            COUNTEROFFER_PROMPT_VERSION,
            EXPLAIN_PROMPT_VERSION,
        ],
        "provider": adapter.provider,
        "model": adapter.model,
        "summary": summary,
        "counteroffer": {
            "status": co.status,
            "note": co.note,
            "model_output": co.proposal.model_dump() if co.proposal else None,
            "deterministic_issues": [i.code for i in co.message_issues],
            "draft_revision": co.draft_revision,
            "requester_view": co.requester_view,
            "model_call": co.model_call.as_dict() if co.model_call else None,
        },
        "explanation": {
            "status": ex.status,
            "text": ex.text,
            "text_source": ex.source,
            "issues": [i.code for i in ex.issues],
            "model_call": ex.model_call.as_dict() if ex.model_call else None,
        },
        "opportunity_explanation": {
            "scenario": "negotiation (buyer view)",
            "recommendation": opp_view["recommendation"],
            "status": opp.status,
            "text": opp.text,
            "text_source": opp.source,
            "issues": [i.code for i in opp.issues],
            "model_call": opp.model_call.as_dict() if opp.model_call else None,
        },
        "cases": rows,
    }
    out = ROOT / "evidence" / "phase3"
    out.mkdir(parents=True, exist_ok=True)
    slug = adapter.model.replace("/", "_").lower() if args.mode == "live" else "reference"
    stem = out / f"ai_eval_{args.mode}_{slug}_{datetime.now(UTC).date().isoformat()}"
    stem.with_suffix(".json").write_text(json.dumps(doc, indent=1, default=str) + "\n", "utf-8")
    md = [
        f"# P3 AI evaluation ({args.mode.upper()})",
        "",
        f"{doc['evidence_class']}. Provider `{adapter.provider}`, model `{adapter.model}`, "
        f"eval `{EVAL_VERSION}`, prompts {', '.join(doc['prompt_versions'])}.",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    md += [f"| {k} | {v} |" for k, v in summary.items()]
    md += [
        "",
        f"Counteroffer: **{co.status}** {co.note}",
        "",
        f"Explanation: **{ex.status}** (text from {ex.source})",
        "",
        f"Opportunity narration (negotiation, buyer): **{opp.status}** (text from {opp.source})",
        "",
        "| Case | Category | Expected | Got | Values |",
        "|---|---|---|---|---|",
    ]
    md += [
        f"| {r['id']} | {r['category']} | {'/'.join(r['expected'])} | {r['status']} | "
        f"{'' if r['values_ok'] is None else ('ok' if r['values_ok'] else 'WRONG')} |"
        for r in rows
    ]
    stem.with_suffix(".md").write_text("\n".join(md) + "\n", "utf-8")
    print(json.dumps(summary, indent=1))
    print(f"wrote {stem.relative_to(ROOT)}.json/.md")
    return 0 if summary["unsafe_passes"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
