"""Brute-force reference for tiny networks: enumerate every assignment, keep what the
independent verifier accepts, rank by `objective_value`. Exponential; tests only."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from api.planning.network import Network, TermsKind
from api.planning.objective import Objective, objective_value
from api.planning.solver import Funding, ScheduledLeg, SolveConfig
from api.planning.verifier import verify

_CAPACITY_ONLY = {"CAPACITY", "REQUIRED_NOT_MET"}


@dataclass(frozen=True, slots=True)
class Reference:
    value: tuple[int, ...]
    schedule: tuple[ScheduledLeg, ...]
    evaluated: int


def _choices(net: Network, allow_proposals: bool, payers: frozenset[str] | None):
    out = []
    for e in sorted(net.obligations, key=lambda e: e.obligation_id):
        opts: list[ScheduledLeg | None] = [None]
        for o in net.options_for(e.obligation_id):
            if o.terms is TermsKind.NEGOTIATED_PROPOSAL and not allow_proposals:
                continue
            if payers is not None and o.payer not in payers:
                continue
            for t in range(len(net.clock)):
                leg = ScheduledLeg(o.option_id, t)
                rep = verify(net, [leg], allow_proposals=allow_proposals)
                if {v.code for v in rep.violations} <= _CAPACITY_ONLY:
                    opts.append(leg)
        out.append(opts)
    return out


def _better(value, legs, best: Reference | None) -> bool:
    if best is None or value > best.value:
        return True
    return value == best.value and _key(legs) < _key(best.schedule)


def _key(legs: tuple[ScheduledLeg, ...]) -> tuple[tuple[int, str], ...]:
    return tuple((x.slot, x.option_id) for x in legs)


def brute_force(net: Network, cfg: SolveConfig, limit: int = 3_000_000) -> Reference | None:
    choices = _choices(net, cfg.allow_proposals, cfg.payers)
    best: Reference | None = None
    n = 0
    for combo in itertools.product(*choices):
        n += 1
        if n > limit:
            raise RuntimeError("brute force limit exceeded")
        legs = tuple(
            sorted((x for x in combo if x is not None), key=lambda x: (x.slot, x.option_id))
        )
        rep = verify(net, legs, allow_proposals=cfg.allow_proposals, required=cfg.required)
        if not rep.ok:
            continue
        value = objective_value(rep.metrics, cfg.objective)
        if _better(value, legs, best):
            best = Reference(value, legs, n)
    return best if best is None else Reference(best.value, best.schedule, n)


def brute_force_min_liquidity(
    net: Network,
    target: frozenset[str],
    funding: tuple[Funding, ...],
    *,
    allow_proposals: bool = False,
    limit: int = 3_000_000,
) -> Reference | None:
    fund = {f.merchant_id: f for f in funding}
    choices = _choices(net, allow_proposals, None)
    best: Reference | None = None
    n = 0
    for combo in itertools.product(*choices):
        n += 1
        if n > limit:
            raise RuntimeError("brute force limit exceeded")
        legs = tuple(
            sorted((x for x in combo if x is not None), key=lambda x: (x.slot, x.option_id))
        )
        probe = verify(net, legs, allow_proposals=allow_proposals, required=target)
        if {v.code for v in probe.violations} - {"CAPACITY"}:
            continue
        allowed = True
        for mo in probe.metrics.merchants:
            f = fund.get(mo.merchant_id)
            if f is None and (mo.existing_draw_cents or mo.hypothetical_need_cents):
                allowed = False
            if f is not None and not f.hypothetical_allowed and mo.hypothetical_need_cents:
                allowed = False
        if not allowed:
            continue
        inject = {
            mo.merchant_id: mo.hypothetical_need_cents
            for mo in probe.metrics.merchants
            if mo.hypothetical_need_cents
        }
        rep = verify(net, legs, allow_proposals=allow_proposals, required=target, injections=inject)
        if not rep.ok:
            continue
        value = objective_value(rep.metrics, Objective.MIN_LIQUIDITY)
        if _better(value, legs, best):
            best = Reference(value, legs, n)
    return best
