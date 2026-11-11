"""Exact small-network planner: dynamic programming over time slots (P2, SIMULATION).

Search space: every assignment of each obligation to "not paid" or to one of its
candidate legs in one slot. The search walks slots in order. At slot t it applies the
receipts that became usable at t, then tries every combination of payments the payers can
afford in that slot, and recurses to t+1. Within a slot the order of payments does not
matter: receipts from slot t are usable only from t+1 (lag >= 1), so the only thing that
changes inside a slot is that payers' cash goes down, and the reserve check after the
slot's last debit is the binding one.

The future depends only on (t, obligations paid, cash per merchant, receipts in transit)
plus, for minimum liquidity, the peak draw per merchant. The objective is additive per leg
(apart from those peaks, which are part of the state), so memoising the best completion
per state is exact. With remaining ties broken by the lexicographically smallest
schedule, the result is unique and deterministic.

Status honesty: OPTIMAL only when the search finished; INFEASIBLE only when it finished
and no plan meets the required obligations. Hitting `max_nodes` returns FEASIBLE with a
greedy incumbent that the caller must verify, or UNKNOWN. It never returns OPTIMAL.

The solver recommends; it never issues a payment, alters an invoice or records consent.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from api.domain.invoice import DisputeStatus
from api.domain.money import USD
from api.planning.network import (
    ConsentBasis,
    FeeStatus,
    Network,
    PaymentOption,
    TermsKind,
)
from api.planning.objective import Objective

SOLVER_NAME = "exact_slot_dp"
SOLVER_VERSION = "p2-solver-1"
SECONDS_PER_YEAR = 365 * 86400
BPS = 10_000
DEFAULT_MAX_NODES = 1_000_000

__all__ = [
    "SOLVER_NAME",
    "SOLVER_VERSION",
    "Funding",
    "MinLiquidityResult",
    "Objective",
    "ScheduledLeg",
    "SolveConfig",
    "SolveResult",
    "SolverStatus",
    "minimum_liquidity",
    "solve",
]


class SolverStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Funding:
    """A merchant whose own existing cash may seed a minimum-liquidity target.

    `hypothetical_allowed` lets the result report *hypothetical external funding* beyond
    that cash. It is a measured gap, never money that exists.
    """

    merchant_id: str
    hypothetical_allowed: bool = False


@dataclass(frozen=True, slots=True)
class SolveConfig:
    objective: Objective = Objective.NETWORK
    allow_proposals: bool = False  # B5: assume both parties approve negotiated proposals
    required: frozenset[str] = frozenset()  # obligations that must be paid by their due slot
    funding: tuple[Funding, ...] = ()  # MIN_LIQUIDITY only
    payers: frozenset[str] | None = None  # restrict who may pay (None = everyone)
    max_nodes: int = DEFAULT_MAX_NODES


@dataclass(frozen=True, slots=True)
class ScheduledLeg:
    option_id: str
    slot: int


def _sort_key(leg: ScheduledLeg) -> tuple[int, str]:
    return (leg.slot, leg.option_id)


@dataclass(frozen=True, slots=True)
class SolveResult:
    status: SolverStatus
    schedule: tuple[ScheduledLeg, ...]
    value: tuple[int, ...] | None
    nodes: int
    complete: bool
    note: str
    solver: str = f"{SOLVER_NAME}/{SOLVER_VERSION}"


@dataclass(frozen=True, slots=True)
class MinLiquidityResult:
    status: SolverStatus
    schedule: tuple[ScheduledLeg, ...]
    value: tuple[int, ...] | None
    existing: dict[str, int]  # peak use of each funding merchant's own existing cash
    hypothetical: dict[str, int]  # HYPOTHETICAL external funding needed (not available)
    target: frozenset[str]
    nodes: int
    complete: bool
    notes: tuple[str, ...] = field(default=())


class _SearchLimitError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Eval:
    option_id: str
    obligation_id: str
    slot: int
    payer: int
    payee: int
    debit: int
    proceeds: int
    arrival: int
    contrib: tuple[int, ...]


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


class _Search:
    def __init__(self, net: Network, cfg: SolveConfig) -> None:
        self.net, self.cfg = net, cfg
        self.H = len(net.clock)
        self.ms = net.merchants
        self.idx = {m: i for i, m in enumerate(self.ms)}
        self.n = len(self.ms)
        self.minliq = cfg.objective is Objective.MIN_LIQUIDITY
        self.base = [net.position(m).base_slack_cents for m in self.ms]
        static_in = [[0] * self.n for _ in range(self.H)]
        for m in self.ms:
            for i in net.position(m).inflows:
                static_in[i.available_slot][self.idx[m]] += i.amount.cents
        # own[t][i]: merchant i's own funds at slot t (opening slack + verified inflows so far)
        self.own: list[list[int]] = []
        run = list(self.base)
        for t in range(self.H):
            run = [r + s for r, s in zip(run, static_in[t], strict=True)]
            self.own.append(list(run))
        fund = {f.merchant_id: f for f in cfg.funding}
        self.own_ok = [m in fund for m in self.ms]
        self.hyp_ok = [m in fund and fund[m].hypothetical_allowed for m in self.ms]
        self.due: dict[str, int | None] = {
            e.obligation_id: net.clock.last_slot_at_or_before(e.due_at) for e in net.obligations
        }
        self.at: dict[str, dict[int, list[_Eval]]] = {}
        for o in net.options:
            if not self._usable(o):
                continue
            for t in range(self.H):
                ev = self._eval(o, t)
                if ev is None:
                    continue
                if o.obligation_id in cfg.required:
                    d = self.due[o.obligation_id]
                    if d is None or t > d:
                        continue  # a late payment does not meet a requirement
                self.at.setdefault(o.obligation_id, {}).setdefault(t, []).append(ev)
        for slots in self.at.values():
            for evs in slots.values():
                evs.sort(key=lambda e: e.option_id)
        self.obls = tuple(sorted(self.at))
        self.last = {oid: max(self.at[oid]) for oid in self.obls}
        self.payer_of = {oid: self.at[oid][self.last[oid]][0].payer for oid in self.obls}
        self.memo: dict[tuple, tuple[tuple[int, ...], tuple] | None] = {}
        self.nodes = 0

    # -- static leg rules -----------------------------------------------------------------
    def _usable(self, o: PaymentOption) -> bool:
        net, cfg = self.net, self.cfg
        e = net.obligation(o.obligation_id)
        pp, pe = net.policy(o.payer), net.policy(o.payee)
        if o.terms is TermsKind.NEGOTIATED_PROPOSAL and not cfg.allow_proposals:
            return False
        if cfg.payers is not None and o.payer not in cfg.payers:
            return False
        if e.dispute is DisputeStatus.OPEN or e.currency != USD:
            return False
        if o.fee.status is FeeStatus.UNKNOWN:
            return False
        if not pp.participating or (o.terms is not TermsKind.IN_FORCE and not pe.participating):
            return False
        auths = o.authorizations
        if any(a.basis is ConsentBasis.REFUSED for a in auths):
            return False
        if any(a.merchant_id not in (o.payer, o.payee) for a in auths):
            return False
        have = {(a.merchant_id, a.action) for a in auths}
        need = {(o.payer, "authorize_payment")}
        if o.terms is TermsKind.OPEN_OFFER:
            need.add((o.payer, "accept_offer"))
        elif o.terms is TermsKind.NEGOTIATED_PROPOSAL:
            need |= {(o.payer, "sign_terms"), (o.payee, "sign_terms")}
        if not need <= have:
            return False
        if pp.max_fee_per_leg is not None and o.fee.payer_fee > pp.max_fee_per_leg:
            return False
        if pe.max_fee_per_leg is not None and o.fee.payee_fee > pe.max_fee_per_leg:
            return False
        if o.payee in pp.prohibited_counterparties or o.payer in pe.prohibited_counterparties:
            return False
        extra = o.concession.cents - e.in_force_concession.cents
        if extra < 0:
            return False
        return not (pe.max_concession_given is not None and extra > pe.max_concession_given.cents)

    def _eval(self, o: PaymentOption, t: int) -> _Eval | None:
        net = self.net
        start = net.clock.start(t)
        if o.expires_at is not None and not start < o.expires_at:
            return None
        valid = net.position(o.payer).valid_until
        if valid is not None and not start < valid:
            return None
        e = net.obligation(o.obligation_id)
        pp, pe = net.policy(o.payer), net.policy(o.payee)
        due = self.due[o.obligation_id]
        on_time = due is not None and t <= due
        early = (
            int((net.clock.start(due) - start).total_seconds())
            if due is not None and t < due
            else 0
        )
        debit = o.cash.cents + o.fee.payer_fee.cents
        proceeds = o.cash.cents - o.fee.payee_fee.cents
        extra = o.concession.cents - e.in_force_concession.cents
        cost = _ceil_div(debit * pp.hurdle_bps * early, BPS * SECONDS_PER_YEAR)
        value = proceeds * pe.early_receipt_value_bps * early // (BPS * SECONDS_PER_YEAR)
        if early and (
            extra < pp.min_early_discount.cents or extra - cost < pp.min_net_benefit_cents
        ):
            return None  # the payer's own rule: paying early must be worth it to the payer
        affected = extra > 0 or early > 0
        rcost = pe.receipt_cost_cents if affected else 0
        floor = pe.min_receipt_net_benefit_cents
        if affected and floor is not None and value - extra - o.fee.payee_fee.cents - rcost < floor:
            return None  # the payee's own rule: changed terms must be worth it to the payee
        focal = net.focal_merchant
        mine = 0
        if o.payer == focal:
            mine += extra - cost - o.fee.payer_fee.cents
        if o.payee == focal:
            mine += -extra + value - o.fee.payee_fee.cents - rcost
        lead = mine if self.cfg.objective is Objective.FOCAL else 0
        contrib = (
            lead,
            0,
            e.face.cents if on_time else 0,
            value - cost - rcost,
            -(debit if early else 0),
            -(debit * early),
            -(o.fee.payer_fee.cents + o.fee.payee_fee.cents),
        )
        return _Eval(
            o.option_id,
            o.obligation_id,
            t,
            self.idx[o.payer],
            self.idx[o.payee],
            debit,
            proceeds,
            t + net.availability.lag(o.payee),
            contrib,
        )

    # -- search -----------------------------------------------------------------------
    def _tick(self) -> None:
        self.nodes += 1
        if self.nodes > self.cfg.max_nodes:
            raise _SearchLimitError

    def _affordable(self, i: int, cash: list[int], own: list[int], spend: list[int]) -> bool:
        if not self.minliq:
            return cash[i] - spend[i] >= 0
        if self.hyp_ok[i]:
            return True
        need = max(0, -own[i]) - (cash[i] - own[i] - spend[i])
        usable = max(0, own[i]) if self.own_ok[i] else 0
        return need <= usable

    def _decisions(
        self, t: int, paid: frozenset[str], cash: list[int], own: list[int]
    ) -> Iterator[list[_Eval]]:
        cands = [
            (oid, self.at[oid][t]) for oid in self.obls if oid not in paid and t in self.at[oid]
        ]
        must = {oid for oid, _ in cands if oid in self.cfg.required and self.last[oid] == t}
        chosen: list[_Eval] = []
        spend = [0] * self.n

        def rec(k: int) -> Iterator[list[_Eval]]:
            if k == len(cands):
                yield list(chosen)
                return
            oid, evs = cands[k]
            if oid not in must:
                yield from rec(k + 1)
            for ev in evs:
                spend[ev.payer] += ev.debit
                if self._affordable(ev.payer, cash, own, spend):
                    chosen.append(ev)
                    yield from rec(k + 1)
                    chosen.pop()
                spend[ev.payer] -= ev.debit

        yield from rec(0)

    def best(self) -> tuple[tuple[int, ...], tuple] | None:
        zero = (0,) * self.n
        peaks = ((0, 0),) * self.n if self.minliq else ()
        return self._best(0, frozenset(), zero, (), peaks)

    def _best(
        self, t: int, paid: frozenset[str], flow: tuple[int, ...], pending: tuple, peaks: tuple
    ) -> tuple[tuple[int, ...], tuple] | None:
        """`flow[i]` = plan inflows already usable - plan outflows, per merchant."""
        for oid in self.cfg.required:
            if oid not in paid and (oid not in self.at or self.last[oid] < t):
                return None
        if t == self.H:
            if self.minliq:
                return ((-sum(h for _, h in peaks), -sum(s for s, _ in peaks), 0, 0, 0, 0, 0), ())
            return ((0,) * 7, ())
        live = {self.payer_of[oid] for oid in self.obls if oid not in paid and self.last[oid] >= t}
        if self.minliq:
            key = (t, paid, flow, pending, peaks)
        else:
            key = (
                t,
                paid,
                tuple(f if i in live else 0 for i, f in enumerate(flow)),
                tuple(p for p in pending if p[1] in live),
            )
        if key in self.memo:
            return self.memo[key]
        self._tick()

        flow_l = list(flow)
        still: list[tuple[int, int, int]] = []
        for arrival, i, cents in pending:
            if arrival == t:
                flow_l[i] += cents
            else:
                still.append((arrival, i, cents))
        own = self.own[t]
        cash = [own[i] + flow_l[i] for i in range(self.n)]

        best: tuple[tuple[int, ...], tuple] | None = None
        for chosen in self._decisions(t, paid, cash, own):
            self._tick()
            nflow = list(flow_l)
            npend = list(still)
            add = [0] * 7
            payers = set()
            for ev in chosen:
                nflow[ev.payer] -= ev.debit
                payers.add(ev.payer)
                if ev.arrival < self.H:
                    npend.append((ev.arrival, ev.payee, ev.proceeds))
                add = [a + c for a, c in zip(add, ev.contrib, strict=True)]
            npeaks = peaks
            if self.minliq:
                npeaks_l = list(peaks)
                ok = True
                for i in payers:
                    need = max(0, -own[i]) - nflow[i]
                    usable = max(0, own[i]) if self.own_ok[i] else 0
                    draw = min(max(0, need), usable)
                    hyp = max(0, need - usable)
                    if hyp and not self.hyp_ok[i]:
                        ok = False
                        break
                    s, h = npeaks_l[i]
                    npeaks_l[i] = (max(s, draw), max(h, hyp))
                if not ok:
                    continue
                npeaks = tuple(npeaks_l)
            nxt = self._best(
                t + 1,
                paid | {ev.obligation_id for ev in chosen},
                tuple(nflow),
                tuple(sorted(npend)),
                npeaks,
            )
            if nxt is None:
                continue
            value = tuple(a + b for a, b in zip(add, nxt[0], strict=True))
            sched = tuple(sorted((t, ev.option_id) for ev in chosen)) + nxt[1]
            if best is None or value > best[0] or (value == best[0] and sched < best[1]):
                best = (value, sched)
        self.memo[key] = best
        return best

    # -- fallback incumbent -----------------------------------------------------------
    def greedy(self) -> tuple[tuple[int, ...], tuple] | None:
        """Earliest-due, cheapest-leg greedy. Only used when the search was cut short."""
        flow = [0] * self.n
        pending: list[tuple[int, int, int]] = []
        paid: set[str] = set()
        value = [0] * 7
        sched: list[tuple[int, str]] = []
        order = sorted(
            self.obls,
            key=lambda o: (
                o not in self.cfg.required,
                self.due[o] if self.due[o] is not None else self.H,
                o,
            ),
        )
        for t in range(self.H):
            for _, i, cents in [p for p in pending if p[0] == t]:
                flow[i] += cents
            pending = [p for p in pending if p[0] != t]
            own = self.own[t]
            for oid in order:
                if oid in paid or t not in self.at[oid]:
                    continue
                for ev in sorted(self.at[oid][t], key=lambda e: (e.debit, e.option_id)):
                    if own[ev.payer] + flow[ev.payer] - ev.debit >= 0:
                        flow[ev.payer] -= ev.debit
                        if ev.arrival < self.H:
                            pending.append((ev.arrival, ev.payee, ev.proceeds))
                        paid.add(oid)
                        sched.append((t, ev.option_id))
                        value = [a + c for a, c in zip(value, ev.contrib, strict=True)]
                        break
        if not self.cfg.required <= paid:
            return None
        return (tuple(value), tuple(sched))


def _legs(sched: tuple) -> tuple[ScheduledLeg, ...]:
    return tuple(sorted((ScheduledLeg(oid, t) for t, oid in sched), key=_sort_key))


def solve(network: Network, config: SolveConfig | None = None) -> SolveResult:
    cfg = config or SolveConfig()
    if cfg.objective is Objective.MIN_LIQUIDITY:
        raise ValueError("use minimum_liquidity() for the minimum-liquidity mode")
    s = _Search(network, cfg)
    try:
        found = s.best()
    except _SearchLimitError:
        inc = s.greedy()
        if inc is None:
            return SolveResult(
                SolverStatus.UNKNOWN,
                (),
                None,
                s.nodes,
                False,
                f"search limit of {cfg.max_nodes} nodes reached; no incumbent meets the "
                "required obligations, so feasibility is unknown",
            )
        return SolveResult(
            SolverStatus.FEASIBLE,
            _legs(inc[1]),
            inc[0],
            s.nodes,
            False,
            f"search limit of {cfg.max_nodes} nodes reached; returning a greedy incumbent "
            "that is feasible but not proven optimal",
        )
    if found is None:
        return SolveResult(
            SolverStatus.INFEASIBLE,
            (),
            None,
            s.nodes,
            True,
            "complete search: no plan pays every required obligation by its due slot",
        )
    return SolveResult(
        SolverStatus.OPTIMAL, _legs(found[1]), found[0], s.nodes, True, "complete search"
    )


def minimum_liquidity(
    network: Network,
    target: frozenset[str],
    funding: tuple[Funding, ...],
    *,
    allow_proposals: bool = False,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> MinLiquidityResult:
    """Minimum existing (then hypothetical) seed cash to pay `target` on time.

    Only `funding` merchants may draw on their own existing cash; everyone else can only
    re-spend what the plan pays them. Hypothetical funding is reported separately and is
    never treated as available. Noncash set-off is out of scope (D011).
    """
    cfg = SolveConfig(
        objective=Objective.MIN_LIQUIDITY,
        allow_proposals=allow_proposals,
        required=frozenset(target),
        funding=tuple(funding),
        max_nodes=max_nodes,
    )
    notes = (
        "existing = peak use of a funding merchant's own verified cash",
        "hypothetical = external funding that does NOT exist; reported only to size a gap",
        "noncash multilateral set-off could need less cash but is a separate legal mode "
        "outside the executable payment model (D011)",
    )
    s = _Search(network, cfg)
    try:
        found = s.best()
    except _SearchLimitError:
        return MinLiquidityResult(
            SolverStatus.UNKNOWN, (), None, {}, {}, frozenset(target), s.nodes, False, notes
        )
    if found is None:
        return MinLiquidityResult(
            SolverStatus.INFEASIBLE, (), None, {}, {}, frozenset(target), s.nodes, True, notes
        )
    legs = _legs(found[1])
    existing, hypothetical = _peaks(s, legs)
    return MinLiquidityResult(
        SolverStatus.OPTIMAL,
        legs,
        found[0],
        existing,
        hypothetical,
        frozenset(target),
        s.nodes,
        True,
        notes,
    )


def _peaks(s: _Search, legs: tuple[ScheduledLeg, ...]) -> tuple[dict[str, int], dict[str, int]]:
    evs = {
        (ev.option_id, ev.slot): ev
        for slots in s.at.values()
        for lst in slots.values()
        for ev in lst
    }
    flow = [0] * s.n
    pending: list[tuple[int, int, int]] = []
    seed, hyp = [0] * s.n, [0] * s.n
    for t in range(s.H):
        for _, i, cents in [p for p in pending if p[0] == t]:
            flow[i] += cents
        pending = [p for p in pending if p[0] != t]
        own = s.own[t]
        payers = set()
        for leg in legs:
            if leg.slot == t:
                ev = evs[(leg.option_id, t)]
                flow[ev.payer] -= ev.debit
                payers.add(ev.payer)
                if ev.arrival < s.H:
                    pending.append((ev.arrival, ev.payee, ev.proceeds))
        for i in payers:
            need = max(0, -own[i]) - flow[i]
            usable = max(0, own[i]) if s.own_ok[i] else 0
            seed[i] = max(seed[i], min(max(0, need), usable))
            hyp[i] = max(hyp[i], max(0, need - usable))
    return (
        {s.ms[i]: v for i, v in enumerate(seed) if v},
        {s.ms[i]: v for i, v in enumerate(hyp) if v},
    )
