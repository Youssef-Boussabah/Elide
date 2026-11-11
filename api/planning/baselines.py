"""B0-B5 baselines on one identical network (doc 12), each checked by the verifier.

All baselines see the same invoices, consents, fees, cash, reserves, deadlines and
availability lag; only the decision rule differs. Non-focal merchants ("responders") use
the B1 rule in B1-B3, so the comparison isolates the focal buyer's choice.

- B0 no acceleration: every merchant pays each invoice on its in-force terms from its
  due slot onward (late if it cannot pay at the due slot). No offers are taken.
- B1 earliest-due-first: every merchant pays as soon as it can, earliest due first (ties by
  invoice id), choosing the cheapest executable terms (in force or an open offer).
- B2 buyer discount only: the focal buyer picks its own payments to maximise its own net
  benefit (discount - time cost - fees), then on-time face of its own invoices, using only
  its own cash. Remaining ties: the smallest schedule. Responders then follow B1.
- B3 random tie-break: like B2, but each buyer choice tied at the optimum is equally
  likely; reports the exact expected outcome over all tied choices (doc 12 tie trap).
- B4 Elide network-aware exact plan (solver, NETWORK objective), no negotiated changes.
- B5 B4 plus negotiated proposals assumed approved by both merchants: a different
  feasible set, stated as such.

Every number here is SYNTHETIC simulation output.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from fractions import Fraction

from api.planning.network import Network, TermsKind
from api.planning.solver import ScheduledLeg, SolveConfig, solve
from api.planning.verifier import Metrics, verify

MAX_FOCAL_COMBINATIONS = 200_000

_STANDARD = frozenset({TermsKind.IN_FORCE, TermsKind.OPEN_OFFER})


@dataclass(frozen=True, slots=True)
class BaselineResult:
    baseline_id: str
    name: str
    schedule: tuple[ScheduledLeg, ...]
    metrics: Metrics
    expected_on_time_face: Fraction
    expected_gross_flow: Fraction
    expected_focal_net: Fraction
    outcomes_considered: int
    notes: tuple[str, ...]
    solver_status: str | None = None


def _due(net: Network, oid: str) -> int | None:
    return net.clock.last_slot_at_or_before(net.obligation(oid).due_at)


def _sorted(legs: list[ScheduledLeg]) -> tuple[ScheduledLeg, ...]:
    return tuple(sorted(legs, key=lambda x: (x.slot, x.option_id)))


def _greedy(
    net: Network,
    *,
    fixed: tuple[ScheduledLeg, ...] = (),
    payers: frozenset[str] | None = None,
    at_due: bool,
    terms: frozenset[TermsKind],
) -> tuple[ScheduledLeg, ...]:
    sched = list(fixed)
    paid = {net.option(x.option_id).obligation_id for x in fixed}
    horizon = len(net.clock)
    order = sorted(
        (e for e in net.obligations if payers is None or e.debtor in payers),
        key=lambda e: (d if (d := _due(net, e.obligation_id)) is not None else -1, e.obligation_id),
    )
    for t in range(horizon):
        for e in order:
            oid = e.obligation_id
            if oid in paid:
                continue
            due = _due(net, oid)
            if at_due and due is not None and t < due:
                continue
            cands = sorted(
                (o for o in net.options_for(oid) if o.terms in terms),
                key=lambda o: (o.cash.cents + o.fee.payer_fee.cents, o.option_id),
            )
            for o in cands:
                trial = [*sched, ScheduledLeg(o.option_id, t)]
                if verify(net, trial).ok:
                    sched = trial
                    paid.add(oid)
                    break
    return _sorted(sched)


def _focal_choices(net: Network) -> list[list[ScheduledLeg | None]]:
    focal = net.focal_merchant
    horizon = len(net.clock)
    out: list[list[ScheduledLeg | None]] = []
    for e in sorted(net.obligations, key=lambda e: e.obligation_id):
        if e.debtor != focal:
            continue
        opts: list[ScheduledLeg | None] = [None]
        for o in net.options_for(e.obligation_id):
            if o.terms not in _STANDARD:
                continue
            for t in range(horizon):
                leg = ScheduledLeg(o.option_id, t)
                if verify(net, [leg]).ok:
                    opts.append(leg)
        out.append(opts)
    return out


def _single(bid: str, name: str, net: Network, sched, notes, status=None) -> BaselineResult:
    m = verify(net, sched, allow_proposals=bid == "B5").metrics
    return BaselineResult(
        bid,
        name,
        sched,
        m,
        Fraction(m.on_time_face),
        Fraction(m.gross_flow),
        Fraction(m.focal_net),
        1,
        tuple(notes),
        status,
    )


def run_baselines(net: Network) -> tuple[BaselineResult, ...]:
    focal = net.focal_merchant
    others = frozenset(m for m in net.merchants if m != focal)
    out: list[BaselineResult] = []

    b0 = _greedy(net, at_due=True, terms=frozenset({TermsKind.IN_FORCE}))
    out.append(_single("B0", "no acceleration", net, b0, ["pay on in-force terms at due"]))
    b1 = _greedy(net, at_due=False, terms=_STANDARD)
    out.append(_single("B1", "earliest-due-first", net, b1, ["ties broken by invoice id"]))

    # B2 / B3: the buyer optimises alone; responders follow B1.
    choices = _focal_choices(net)
    total = 1
    for c in choices:
        total *= len(c)
    if total > MAX_FOCAL_COMBINATIONS:
        raise ValueError(f"B2 enumeration too large ({total} combinations)")
    best_key: tuple[int, int] | None = None
    ties: list[tuple[ScheduledLeg, ...]] = []
    for combo in itertools.product(*choices):
        legs = _sorted([x for x in combo if x is not None])
        rep = verify(net, legs)
        if not rep.ok:
            continue
        own_face = sum(
            net.obligation(r.obligation_id).face.cents for r in rep.metrics.legs if r.on_time
        )
        key = (rep.metrics.focal_net, own_face)
        if best_key is None or key > best_key:
            best_key, ties = key, [legs]
        elif key == best_key:
            ties.append(legs)
    ties.sort(key=lambda s: tuple((x.slot, x.option_id) for x in s))
    outcomes = [
        _greedy(net, fixed=tie, payers=others, at_due=False, terms=_STANDARD) for tie in ties
    ]
    out.append(
        _single(
            "B2",
            "buyer discount only",
            net,
            outcomes[0],
            [f"{len(ties)} tied buyer choice(s); deterministic smallest-schedule tie-break"],
        )
    )
    ms = [verify(net, s).metrics for s in outcomes]
    k = len(ms)
    out.append(
        BaselineResult(
            "B3",
            "random tie-break (expected)",
            outcomes[0],
            ms[0],
            Fraction(sum(m.on_time_face for m in ms), k),
            Fraction(sum(m.gross_flow for m in ms), k),
            Fraction(sum(m.focal_net for m in ms), k),
            k,
            (f"exact expectation over {k} equally likely tied buyer choice(s)",),
        )
    )

    r4 = solve(net, SolveConfig())
    out.append(
        _single(
            "B4",
            "Elide network-aware",
            net,
            r4.schedule,
            ["exact solver, no negotiated changes"],
            r4.status.value,
        )
    )
    has_proposals = any(o.terms is TermsKind.NEGOTIATED_PROPOSAL for o in net.options)
    r5 = solve(net, SolveConfig(allow_proposals=True))
    note = (
        "different feasible set: negotiated proposals assumed approved by both merchants "
        "(SYNTHETIC assumption)"
        if has_proposals
        else "no negotiated change available: B5 equals B4 (same feasible set)"
    )
    out.append(
        _single("B5", "Elide with negotiated change", net, r5.schedule, [note], r5.status.value)
    )
    return tuple(out)
