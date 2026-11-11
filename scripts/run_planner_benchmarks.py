"""P2 planner evidence: canonical scenarios + seeded synthetic B0-B5 benchmark.

Everything here is SYNTHETIC simulation (evidence class "Synthetic-tested"), except the
CAD settlement fact and the USD 29.01 fee example, which come from the redacted Sandbox
run files. No network access, no PayPal call, no database.

    uv run python scripts/run_planner_benchmarks.py [--seeds 40] [--out evidence/phase2]

Writes `planner_benchmark_<UTC date>.json` (full numbers) and `.md` (summary). Solve times
are machine-specific and reported separately from the deterministic results.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.planning.baselines import run_baselines  # noqa: E402
from api.planning.generator import FAMILIES, GENERATOR_VERSION, generate  # noqa: E402
from api.planning.reference import brute_force  # noqa: E402
from api.planning.solver import (  # noqa: E402
    SOLVER_VERSION,
    Funding,
    SolveConfig,
    SolverStatus,
    minimum_liquidity,
    solve,
)
from api.planning.verifier import VERIFIER_VERSION, verify  # noqa: E402
from fixtures import planning as fx  # noqa: E402


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def dist(xs: list[float]) -> dict[str, float | int]:
    return {
        "n": len(xs),
        "median": pct(xs, 0.5),
        "p25": pct(xs, 0.25),
        "p75": pct(xs, 0.75),
        "p5": pct(xs, 0.05),
        "p95": pct(xs, 0.95),
        "mean": statistics.fmean(xs) if xs else 0.0,
        "zero_fraction": sum(1 for x in xs if x == 0) / len(xs) if xs else 0.0,
        "negative_fraction": sum(1 for x in xs if x < 0) / len(xs) if xs else 0.0,
        "positive_fraction": sum(1 for x in xs if x > 0) / len(xs) if xs else 0.0,
    }


def usd(cents: Fraction | int) -> str:
    v = Fraction(cents) / 100
    return f"${float(v):,.2f}"


def canonical() -> list[dict]:
    rows = []
    cases = [
        ("A two candidates", fx.network_two_candidates(), False, frozenset()),
        ("B negotiation (B4: no change)", fx.network_negotiation(), False, frozenset()),
        ("B negotiation (B5: $10 assumed approved)", fx.network_negotiation(), True, frozenset()),
        ("C held receipt", fx.network_held_receipt(), False, frozenset({"freight_001"})),
        ("D reserve shortfall", fx.network_reserve_shortfall(), False, frozenset({"freight_001"})),
        (
            "D' hypothetical USD net after the Sandbox fee",
            fx.network_reserve_shortfall(hypothetical_usd_net=True),
            False,
            frozenset({"freight_001"}),
        ),
        (
            "F payee fee USD 29.01 in the cascade",
            fx.network_fee_cascade(),
            False,
            frozenset({"food_001", "freight_001"}),
        ),
        ("E CAD settlement credit", fx.network_cad_settlement(), False, frozenset({"freight_001"})),
        (
            "G positive-sum example (CONSTRUCTED, disclosed assumptions)",
            fx.network_positive_sum(),
            False,
            frozenset(),
        ),
        (
            "H invoice partially paid before planning",
            fx.network_partially_paid(),
            False,
            frozenset(),
        ),
        (
            "I timing matters (CONSTRUCTED, not representative)",
            fx.network_timing_matters(),
            False,
            frozenset(),
        ),
    ]
    for name, net, proposals, required in cases:
        r = solve(net, SolveConfig(allow_proposals=proposals, required=required))
        rep = verify(net, r.schedule, allow_proposals=proposals, required=required)
        row = {
            "scenario": name,
            "network_id": net.network_id,
            "labels": list(net.labels),
            "solver_status": r.status.value,
            "schedule": [f"{x.option_id}@{x.slot}" for x in r.schedule],
            "verifier_ok": rep.ok,
            "on_time_face_cents": rep.metrics.on_time_face,
            "gross_flow_cents": rep.metrics.gross_flow,
            "downstream_face_cents": rep.metrics.downstream_face,
            "focal_net_cents": rep.metrics.focal_net,
            "open_discharged_cents": rep.metrics.discharged_open,
            "prior_cash_excluded_cents": rep.metrics.prior_cash_excluded,
            "net_benefit_by_participant": {
                o.merchant_id: o.net_benefit
                for o in rep.metrics.merchants
                if any(o.merchant_id in (x.payer, x.payee) for x in rep.metrics.legs)
            },
        }
        if not required:
            row["baselines"] = {
                b.baseline_id: str(b.expected_on_time_face) for b in run_baselines(net)
            }
        if r.status is SolverStatus.INFEASIBLE:
            funding = tuple(Funding(m, hypothetical_allowed=True) for m in net.merchants)
            ml = minimum_liquidity(net, required, funding)
            probe = verify(net, ml.schedule, required=required)
            row["why"] = [v.detail for v in probe.violations if v.code == "CAPACITY"]
            row["min_liquidity"] = {
                "existing_cents": ml.existing,
                "hypothetical_cents": ml.hypothetical,
                "label": "HYPOTHETICAL funding does not exist",
            }
        rows.append(row)
    chain = fx.network_two_candidates()
    ml = minimum_liquidity(
        chain, frozenset({"food_001", "freight_001", "repair_001"}), (Funding("buyer"),)
    )
    rows.append(
        {
            "scenario": "minimum existing seed for food -> freight -> repair",
            "status": ml.status.value,
            "existing_cents": ml.existing,
            "hypothetical_cents": ml.hypothetical,
        }
    )
    loop = fx.network_loop()
    ml = minimum_liquidity(
        loop,
        frozenset({"ab_001", "bc_001", "ca_001"}),
        tuple(Funding(m, hypothetical_allowed=True) for m in ("a", "b", "c")),
    )
    rows.append(
        {
            "scenario": "A->B->C->A $100 loop, no cash (doc 05)",
            "status": ml.status.value,
            "hypothetical_cents": ml.hypothetical,
            "note": "noncash set-off could need $0 but is out of the payment model (D011)",
        }
    )
    return rows


def benchmark(seeds: int) -> dict:
    per_network = []
    times: list[float] = []
    for fam in FAMILIES:
        for seed in range(seeds):
            net = generate(fam, seed, max_obligations=6, slots=4)
            t0 = time.perf_counter()
            res = {b.baseline_id: b for b in run_baselines(net)}
            times.append(time.perf_counter() - t0)
            per_network.append(
                {
                    "family": fam,
                    "seed": seed,
                    "legs": len(net.options),
                    "b4_status": res["B4"].solver_status,
                    **{f"{k}_on_time": float(v.expected_on_time_face) for k, v in res.items()},
                    **{f"{k}_gross": float(v.expected_gross_flow) for k, v in res.items()},
                    **{f"{k}_focal_net": float(v.expected_focal_net) for k, v in res.items()},
                    **{
                        f"{k}_discharged": float(v.expected_on_time_discharged)
                        for k, v in res.items()
                    },
                }
            )
    agree = checked = 0
    for fam in FAMILIES:
        for seed in range(seeds):
            net = generate(fam, seed, max_obligations=4, slots=3)
            for proposals in (False, True):
                cfg = SolveConfig(allow_proposals=proposals)
                r, ref = solve(net, cfg), brute_force(net, cfg)
                checked += 1
                same = ref is not None and (r.value, r.schedule) == (ref.value, ref.schedule)
                agree += int(same)

    def col(a: str, b: str) -> list[float]:
        return [n[a] - n[b] for n in per_network]

    return {
        "generator": GENERATOR_VERSION,
        "families": list(FAMILIES),
        "seeds_per_family": seeds,
        "size": {"max_obligations": 6, "slots": 4},
        "networks": len(per_network),
        "b4_statuses": sorted({n["b4_status"] for n in per_network}),
        "lift_on_time_cents": {
            "B4_minus_B0": dist(col("B4_on_time", "B0_on_time")),
            "B4_minus_B1": dist(col("B4_on_time", "B1_on_time")),
            "B4_minus_B2": dist(col("B4_on_time", "B2_on_time")),
            "B4_minus_B3": dist(col("B4_on_time", "B3_on_time")),
            "B5_minus_B4": dist(col("B5_on_time", "B4_on_time")),
        },
        "lift_on_time_discharged_cents": {
            "B4_minus_B1": dist(col("B4_discharged", "B1_discharged")),
            "B4_minus_B3": dist(col("B4_discharged", "B3_discharged")),
        },
        "gross_flow_minus_on_time_B4_cents": dist(col("B4_gross", "B4_on_time")),
        "buyer_net_benefit_cents": {
            "B4_minus_B2": dist(col("B4_focal_net", "B2_focal_net")),
            "B5_minus_B2": dist(col("B5_focal_net", "B2_focal_net")),
        },
        "solver_vs_brute_force": {"checked": checked, "identical_value_and_schedule": agree},
        "timing_seconds_all_baselines_per_network": {
            "median": pct(times, 0.5),
            "max": max(times),
            "note": "machine-specific wall clock; not part of the deterministic results",
        },
        "per_network": per_network,
    }


def summary(doc: dict) -> str:
    b = doc["benchmark"]
    lines = [
        "# P2 planner benchmark (SYNTHETIC)",
        "",
        f"Generated {doc['generated_at']} by `scripts/run_planner_benchmarks.py` at commit "
        f"`{doc['commit']}`; solver `{SOLVER_VERSION}`, verifier `{VERIFIER_VERSION}`, "
        f"generator `{b['generator']}`. Evidence class: **Synthetic-tested**. No PayPal call.",
        "",
        "## Canonical scenarios",
        "",
        "| Scenario | Solver | Plan | On-time face | Gross flow | B0/B1/B2/B3/B4/B5 on-time |",
        "|---|---|---|---|---|---|",
    ]
    for r in doc["canonical"]:
        if "solver_status" not in r:
            continue
        bl = r.get("baselines")
        bl_s = " / ".join(usd(Fraction(bl[k])) for k in sorted(bl)) if bl else "-"
        lines.append(
            f"| {r['scenario']} | {r['solver_status']} | {', '.join(r['schedule']) or '(none)'} "
            f"| {usd(r['on_time_face_cents'])} | {usd(r['gross_flow_cents'])} | {bl_s} |"
        )
    lines += ["", "Infeasible targets (explained, with HYPOTHETICAL gap sizing):", ""]
    for r in doc["canonical"]:
        if "why" in r:
            gaps = r["min_liquidity"]["hypothetical_cents"].items()
            hyp = ", ".join(f"{m} {usd(c)}" for m, c in gaps)
            lines.append(
                f"- {r['scenario']}: {'; '.join(r['why']) or 'see min-liquidity'} "
                f"(hypothetical: {hyp or 'none'})"
            )
    for r in doc["canonical"]:
        if "solver_status" not in r:
            lines.append(
                f"- {r['scenario']}: {r['status']}, existing "
                f"{r.get('existing_cents', {})}, hypothetical {r['hypothetical_cents']}"
            )
    lb = b["lift_on_time_cents"]
    bn = b["buyer_net_benefit_cents"]["B4_minus_B2"]
    lines += [
        "",
        "## Headline findings (read these first)",
        "",
        f"- B4 beats earliest-due-first (B1) in only {lb['B4_minus_B1']['positive_fraction']:.0%} "
        "of generated networks.",
        f"- B4 beats the random tie-break buyer baseline (B3) in "
        f"{lb['B4_minus_B3']['positive_fraction']:.0%}.",
        "- The negotiated change (B5) improves on B4 in "
        f"{lb['B5_minus_B4']['positive_fraction']:.0%}.",
        f"- Under B4 the buyer's modeled net benefit is below its own best plan (B2) in "
        f"{bn['negative_fraction']:.0%} of networks.",
        "- Constructed examples (G, I) show mechanisms; they are not representative.",
        "- In the canonical scenario the supplier's modeled net benefit is -$10.00: not a win-win.",
    ]
    lines += [
        "",
        f"## Seeded synthetic networks ({b['networks']} networks: {len(b['families'])} families "
        f"x {b['seeds_per_family']} seeds, <= {b['size']['max_obligations']} obligations, "
        f"{b['size']['slots']} slots)",
        "",
        "Lift in on-time invoice face (cents) per network, paired on identical inputs:",
        "",
        "| Comparison | median | p25 | p75 | p5 | p95 | mean | zero | negative | positive |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows = {
        **b["lift_on_time_cents"],
        **{f"discharged {k}": v for k, v in b.get("lift_on_time_discharged_cents", {}).items()},
        **{f"buyer net {k}": v for k, v in b["buyer_net_benefit_cents"].items()},
    }
    for k, d in rows.items():
        lines.append(
            f"| {k} | {d['median']:.0f} | {d['p25']:.0f} | {d['p75']:.0f} | {d['p5']:.0f} | "
            f"{d['p95']:.0f} | {d['mean']:.0f} | {d['zero_fraction']:.0%} | "
            f"{d['negative_fraction']:.0%} | {d['positive_fraction']:.0%} |"
        )
    sv = b["solver_vs_brute_force"]
    lines += [
        "",
        f"Solver vs brute force (<= 4 obligations, 3 slots, with and without proposals): "
        f"{sv['identical_value_and_schedule']} / {sv['checked']} identical value and schedule.",
        f"B4 solver statuses: {', '.join(b['b4_statuses'])}.",
        f"Median time for all six baselines per network: "
        f"{b['timing_seconds_all_baselines_per_network']['median']:.3f} s "
        f"(max {b['timing_seconds_all_baselines_per_network']['max']:.3f} s; machine-specific).",
        "",
        "Reading: B4 >= B0..B3 on on-time face by construction (every baseline schedule is in the",
        "planner's feasible set). Zero lift is the common case; positive lift needs a real",
        "choice between candidate payments. B5 changes the feasible set (assumed approvals).",
        "Buyer net benefit can be lower under B4 than under B2: the network plan is shown",
        "next to the buyer's own best plan and the buyer's minimum benefit is still enforced.",
    ]
    if doc.get("reproduces"):
        r = doc["reproduces"]
        lines += [
            "",
            "## Reproducibility",
            "",
            f"Original-face results reproduced against `{r['reference']}`: "
            f"{r['identical_networks']} / {r['networks']} networks identical on every B0-B5 "
            "on-time face, gross flow and buyer net benefit value.",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=40)
    ap.add_argument("--out", default=str(ROOT / "evidence" / "phase2"))
    ap.add_argument("--tag", default="", help="suffix for the output file names")
    ap.add_argument("--compare", default="", help="earlier JSON whose face results must match")
    args = ap.parse_args()
    import subprocess

    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    ).stdout.strip()
    now = datetime.now(UTC)
    doc = {
        "evidence_class": "Synthetic-tested",
        "generated_at": now.isoformat(timespec="seconds"),
        "commit": commit,
        "solver": SOLVER_VERSION,
        "verifier": VERIFIER_VERSION,
        "canonical": canonical(),
        "benchmark": benchmark(args.seeds),
    }
    if args.compare:
        ref = json.loads(Path(args.compare).read_text("utf-8"))["benchmark"]["per_network"]
        keys = [k for k in ref[0] if k.endswith(("_on_time", "_gross", "_focal_net"))]
        new = {(n["family"], n["seed"]): n for n in doc["benchmark"]["per_network"]}
        same = sum(all(new[(r["family"], r["seed"])][k] == r[k] for k in keys) for r in ref)
        doc["reproduces"] = {
            "reference": Path(args.compare).name,
            "networks": len(ref),
            "identical_networks": same,
            "fields": keys,
        }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"planner_benchmark_{now.date().isoformat()}{args.tag}"
    stem.with_suffix(".json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", "utf-8")
    stem.with_suffix(".md").write_text(summary(doc), "utf-8")
    print(summary(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
