"""Independent replay verifier for P2 plans (does not use the solver).

Given a network and a proposed schedule [(leg_id, slot), ...] it replays the plan slot by
slot with an explicit per-merchant ledger and recomputes everything from the network
data alone:

1. Leg rules: the leg exists, appears once, settles its invoice at most once and exactly
   (cash + concession + already applied = face); slot within the horizon, the offer
   window and the payer's evidence validity; USD only; no open dispute; fees known or
   bounded and within each merchant's ceiling; consent not refused; only the payer and
   payee authorize (one merchant can never sign for another), with every approval the
   terms need; proposals only when explicitly enabled (B5); counterparties allowed; the
   payer's own acceleration rule (minimum discount and net benefit after time cost) and
   the payee's concession cap.
2. Cash replay: at each slot the receipts that became usable at that slot are applied
   first, then the legs in leg-id order. After *every* debit (amount + payer fee) the
   payer must keep `cash - reserve - committed >= 0`. Proceeds (amount - payee fee) are
   usable only from `slot + lag` (lag >= 1, so never in the same slot).
3. Conservation: opening + verified inflows + injections = closing + fees + in transit.
4. Deadlines: on time means paid in a slot that starts no later than the due instant.
   Required obligations must be paid on time.
5. Metrics: on-time face, gross flow, fees, early cash, cash x time, per-merchant
   discounts, fees, time cost, stated early-receipt value and net benefit, own-funds draw
   and hypothetical funding need.

`injections` are HYPOTHETICAL external funding added at slot 0 to test minimum-liquidity
results. They are reported separately and never count as existing cash.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction

from api.domain.invoice import DisputeStatus
from api.domain.money import USD, Money
from api.planning.network import (
    ConsentBasis,
    FeeStatus,
    Network,
    PaymentOption,
    TermsKind,
)

VERIFIER_VERSION = "p2-verifier-1"
_YEAR_SECONDS = 31_536_000  # 365 days
_BPS = 10_000


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    leg_id: str | None
    slot: int | None
    merchant: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class LegReport:
    leg_id: str
    obligation_id: str
    payer: str
    payee: str
    slot: int
    amount_cents: int
    concession_cents: int
    extra_concession_cents: int
    payer_fee_cents: int
    payee_fee_cents: int
    debit_cents: int
    proceeds_cents: int
    usable_slot: int | None  # None: proceeds become usable after the horizon
    on_time: bool
    accelerated: bool
    seconds_early: int
    time_cost_cents: int
    receipt_value_cents: int


@dataclass(frozen=True, slots=True)
class MerchantOutcome:
    merchant_id: str
    opening_cents: int
    closing_cents: int
    min_slack_cents: int | None  # lowest cash - reserve - committed after its own debits
    min_slack_slot: int | None
    discount_gained: int
    discount_given: int
    fees_paid: int
    opportunity_cost: int
    early_receipt_value: int
    net_benefit: int
    existing_draw_cents: int  # peak use of its own funds
    hypothetical_need_cents: int  # peak gap its own funds could not cover
    hypothetical_cents: int  # hypothetical injection supplied to this replay
    in_transit_cents: int


@dataclass(frozen=True, slots=True)
class Metrics:
    on_time_face: int
    on_time_count: int
    gross_flow: int
    fees: int
    early_cash: int
    cent_seconds: int
    surplus: int
    focal_net: int
    downstream_face: int
    in_transit: int
    external_pending: int
    required_met: bool
    merchants: tuple[MerchantOutcome, ...]
    legs: tuple[LegReport, ...]

    @property
    def liquidity_dollar_days(self) -> Fraction:
        return Fraction(self.cent_seconds, 100 * 86_400)

    def merchant(self, merchant_id: str) -> MerchantOutcome:
        return next(m for m in self.merchants if m.merchant_id == merchant_id)


@dataclass(frozen=True, slots=True)
class VerificationReport:
    ok: bool
    violations: tuple[Violation, ...]
    metrics: Metrics
    verifier: str = VERIFIER_VERSION


def _usd(cents: int) -> str:
    return str(Money(cents, USD)) if cents >= 0 else f"-{Money(-cents, USD)}"


def _leg_items(schedule: Iterable) -> list[tuple[str, int]]:
    out = []
    for item in schedule:
        if isinstance(item, tuple):
            out.append((str(item[0]), int(item[1])))
        else:
            out.append((item.option_id, item.slot))
    return out


def _due_slot(net: Network, oid: str) -> int | None:
    due_at = net.obligation(oid).due_at
    slots = [t for t in range(len(net.clock)) if net.clock.start(t) <= due_at]
    return slots[-1] if slots else None


def _static_problems(
    net: Network, o: PaymentOption, slot: int, allow_proposals: bool
) -> list[tuple[str, str]]:
    """Every rule a single leg must satisfy regardless of cash."""
    p: list[tuple[str, str]] = []
    e = net.obligation(o.obligation_id)
    payer_pol, payee_pol = net.policy(o.payer), net.policy(o.payee)
    when = net.clock.start(slot)
    if o.terms is TermsKind.NEGOTIATED_PROPOSAL and not allow_proposals:
        p.append(("PROPOSAL_NOT_ENABLED", "negotiated proposal used without enabling B5"))
    if e.currency != USD or o.cash.currency != USD:
        p.append(("CURRENCY_BLOCKED", f"{e.currency} obligation in a USD-only plan (D008)"))
    if e.dispute is DisputeStatus.OPEN:
        p.append(("DISPUTED", f"{e.obligation_id} has an open dispute"))
    if o.cash.cents + o.concession.cents + e.cash_applied.cents != e.face.cents:
        p.append(("INVOICE_IDENTITY", "cash + concession + applied != face"))
    if o.terms is TermsKind.IN_FORCE and o.concession != e.in_force_concession:
        p.append(("TERMS_MISMATCH", "in-force leg with a different concession"))
    if o.expires_at is not None and when >= o.expires_at:
        p.append(("OFFER_EXPIRED", f"slot {slot} starts at/after the offer deadline"))
    valid = net.position(o.payer).valid_until
    if valid is not None and when >= valid:
        p.append(("EVIDENCE_EXPIRED", f"{o.payer}'s capacity evidence expired before slot {slot}"))
    if o.fee.status is FeeStatus.UNKNOWN:
        p.append(("FEE_UNKNOWN", "fee has no amount or ceiling"))
    if payer_pol.max_fee_per_leg is not None and o.fee.payer_fee > payer_pol.max_fee_per_leg:
        p.append(("FEE_ABOVE_CEILING", f"{o.payer} fee ceiling"))
    if payee_pol.max_fee_per_leg is not None and o.fee.payee_fee > payee_pol.max_fee_per_leg:
        p.append(("FEE_ABOVE_CEILING", f"{o.payee} fee ceiling"))
    if not payer_pol.participating or (
        o.terms is not TermsKind.IN_FORCE and not payee_pol.participating
    ):
        p.append(("NOT_PARTICIPATING", "a required merchant does not participate"))
    if (
        o.payee in payer_pol.prohibited_counterparties
        or o.payer in payee_pol.prohibited_counterparties
    ):
        p.append(("PROHIBITED_COUNTERPARTY", f"{o.payer} -> {o.payee} not allowed"))
    # consent: only the two parties, nobody refused, every needed approval present
    if any(a.basis is ConsentBasis.REFUSED for a in o.authorizations):
        p.append(("CONSENT_REFUSED", "a party declined"))
    if any(a.merchant_id not in (o.payer, o.payee) for a in o.authorizations):
        p.append(("FOREIGN_AUTHORIZATION", "an authorization comes from a third merchant"))
    granted = {
        (a.merchant_id, a.action)
        for a in o.authorizations
        if a.basis is not ConsentBasis.REFUSED and a.merchant_id in (o.payer, o.payee)
    }
    if (o.payer, "authorize_payment") not in granted:
        p.append(("PAYER_AUTHORIZATION_MISSING", f"{o.payer} must authorize its own payment"))
    if o.terms is TermsKind.OPEN_OFFER and (o.payer, "accept_offer") not in granted:
        p.append(("ACCEPTANCE_MISSING", f"{o.payer} must accept the offer"))
    if (
        o.terms is TermsKind.NEGOTIATED_PROPOSAL
        and not {
            (o.payer, "sign_terms"),
            (o.payee, "sign_terms"),
        }
        <= granted
    ):
        p.append(("ACCEPTANCE_MISSING", "both parties must sign the proposed terms"))
    extra = o.concession.cents - e.in_force_concession.cents
    if payee_pol.max_concession_given is not None and extra > payee_pol.max_concession_given.cents:
        p.append(("PAYEE_REJECTS", f"{o.payee} concession cap exceeded"))
    if extra < 0:
        p.append(("PAYER_REJECTS", "terms worse than those in force"))
    return p


def verify(
    network: Network,
    schedule: Iterable,
    *,
    allow_proposals: bool = False,
    required: frozenset[str] = frozenset(),
    injections: Mapping[str, int] | None = None,
) -> VerificationReport:
    net = network
    inject = dict(injections or {})
    for m, cents in inject.items():
        if m not in net.merchants:
            raise ValueError(f"injection for unknown merchant {m}")
        if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
            raise ValueError("hypothetical injections must be positive int cents")
    horizon = len(net.clock)
    bad: list[Violation] = []

    # -- 1. structure and per-leg rules -----------------------------------------------
    accepted: list[tuple[int, str]] = []
    seen_leg: set[str] = set()
    seen_obl: dict[str, str] = {}
    for leg_id, slot in _leg_items(schedule):
        if not net.has_option(leg_id):
            bad.append(Violation("UNKNOWN_LEG", leg_id, slot, None, "no such leg"))
            continue
        if not 0 <= slot < horizon:
            bad.append(Violation("SLOT_OUT_OF_RANGE", leg_id, slot, None, f"horizon {horizon}"))
            continue
        if leg_id in seen_leg:
            bad.append(Violation("DUPLICATE_LEG", leg_id, slot, None, "leg scheduled twice"))
            continue
        seen_leg.add(leg_id)
        o = net.option(leg_id)
        if o.obligation_id in seen_obl:
            other = seen_obl[o.obligation_id]
            bad.append(
                Violation(
                    "DOUBLE_SETTLEMENT",
                    leg_id,
                    slot,
                    o.payer,
                    f"{o.obligation_id} already settled by {other}",
                )
            )
            continue
        seen_obl[o.obligation_id] = leg_id
        for code, detail in _static_problems(net, o, slot, allow_proposals):
            bad.append(Violation(code, leg_id, slot, o.payer, detail))
        accepted.append((slot, leg_id))
    accepted.sort()

    # -- per-leg economics (payer acceleration rule) ------------------------------------
    reports: dict[str, LegReport] = {}
    for slot, leg_id in accepted:
        o = net.option(leg_id)
        e = net.obligation(o.obligation_id)
        payer_pol, payee_pol = net.policy(o.payer), net.policy(o.payee)
        due = _due_slot(net, o.obligation_id)
        on_time = due is not None and slot <= due
        accelerated = due is not None and slot < due
        secs = (
            int((net.clock.start(due) - net.clock.start(slot)).total_seconds())
            if accelerated and due is not None
            else 0
        )
        debit = o.cash.cents + o.fee.payer_fee.cents
        proceeds = o.cash.cents - o.fee.payee_fee.cents
        extra = o.concession.cents - e.in_force_concession.cents
        num = debit * payer_pol.hurdle_bps * secs
        cost = num // (_BPS * _YEAR_SECONDS) + (1 if num % (_BPS * _YEAR_SECONDS) else 0)
        value = (proceeds * payee_pol.early_receipt_value_bps * secs) // (_BPS * _YEAR_SECONDS)
        if accelerated:
            if extra < payer_pol.min_early_discount.cents:
                bad.append(
                    Violation(
                        "PAYER_REJECTS",
                        leg_id,
                        slot,
                        o.payer,
                        f"paying early needs >= {_usd(payer_pol.min_early_discount.cents)} "
                        f"extra discount, terms give {_usd(extra)}",
                    )
                )
            elif extra - cost < payer_pol.min_net_benefit_cents:
                bad.append(
                    Violation(
                        "PAYER_REJECTS",
                        leg_id,
                        slot,
                        o.payer,
                        f"discount {_usd(extra)} - time cost {_usd(cost)} is below the "
                        f"payer's minimum net benefit",
                    )
                )
        lag = net.availability.lag(o.payee)
        reports[leg_id] = LegReport(
            leg_id=leg_id,
            obligation_id=o.obligation_id,
            payer=o.payer,
            payee=o.payee,
            slot=slot,
            amount_cents=o.cash.cents,
            concession_cents=o.concession.cents,
            extra_concession_cents=extra,
            payer_fee_cents=o.fee.payer_fee.cents,
            payee_fee_cents=o.fee.payee_fee.cents,
            debit_cents=debit,
            proceeds_cents=proceeds,
            usable_slot=slot + lag if slot + lag < horizon else None,
            on_time=on_time,
            accelerated=accelerated,
            seconds_early=secs,
            time_cost_cents=cost,
            receipt_value_cents=value,
        )

    # -- 2. cash replay -------------------------------------------------------------------
    usable_cash = {m: net.position(m).available.cents + inject.get(m, 0) for m in net.merchants}
    held_back = {
        m: net.position(m).protected_reserve.cents + net.position(m).committed.cents
        for m in net.merchants
    }
    own_base = {m: net.position(m).base_slack_cents for m in net.merchants}
    external_total = 0
    arrivals: dict[int, list[tuple[str, int, str]]] = defaultdict(list)
    for m in net.merchants:
        for inflow in net.position(m).inflows:
            arrivals[inflow.available_slot].append((m, inflow.amount.cents, "external"))
    by_slot: dict[int, list[str]] = defaultdict(list)
    for slot, leg_id in accepted:
        by_slot[slot].append(leg_id)
    plan_net = dict.fromkeys(net.merchants, 0)  # plan receipts usable - plan debits
    own_now = dict(own_base)
    min_slack: dict[str, tuple[int, int]] = {}
    draw = dict.fromkeys(net.merchants, 0)
    need_peak = dict.fromkeys(net.merchants, 0)
    fees = 0
    in_transit = dict.fromkeys(net.merchants, 0)
    for t in range(horizon):
        for m, cents, kind in arrivals.get(t, []):
            usable_cash[m] += cents
            if kind == "external":
                own_now[m] += cents
                external_total += cents
            else:
                plan_net[m] += cents
        for leg_id in sorted(by_slot.get(t, [])):
            r = reports[leg_id]
            before = usable_cash[r.payer]
            usable_cash[r.payer] = before - r.debit_cents
            plan_net[r.payer] -= r.debit_cents
            fees += r.payer_fee_cents + r.payee_fee_cents
            slack = usable_cash[r.payer] - held_back[r.payer]
            if r.payer not in min_slack or slack < min_slack[r.payer][0]:
                min_slack[r.payer] = (slack, t)
            if slack < 0:
                pos = net.position(r.payer)
                committed = (
                    f" + {_usd(pos.committed.cents)} committed" if pos.committed.cents else ""
                )
                bad.append(
                    Violation(
                        "CAPACITY",
                        leg_id,
                        t,
                        r.payer,
                        f"{r.payer} needs {_usd(r.debit_cents)} + "
                        f"{_usd(pos.protected_reserve.cents)} reserve{committed}, has "
                        f"{_usd(before)} available at slot {t}: short by {_usd(-slack)}",
                    )
                )
            # own-funds accounting (independent of any injection)
            shortfall_before_plan = max(0, -own_now[r.payer])
            need = shortfall_before_plan - plan_net[r.payer]
            usable_own = max(0, own_now[r.payer])
            draw[r.payer] = max(draw[r.payer], min(max(0, need), usable_own))
            need_peak[r.payer] = max(need_peak[r.payer], max(0, need - usable_own))
            if r.usable_slot is None:
                in_transit[r.payee] += r.proceeds_cents
            else:
                arrivals[r.usable_slot].append((r.payee, r.proceeds_cents, "plan"))

    # -- 3. conservation -------------------------------------------------------------------
    opening = sum(net.position(m).available.cents for m in net.merchants)
    closing = sum(usable_cash.values())
    if closing + fees + sum(in_transit.values()) != opening + external_total + sum(inject.values()):
        bad.append(Violation("CONSERVATION", None, None, None, "cash not conserved"))

    # -- 4. deadlines ------------------------------------------------------------------
    on_time_obls = {r.obligation_id for r in reports.values() if r.on_time}
    missing = sorted(required - on_time_obls)
    for oid in missing:
        bad.append(Violation("REQUIRED_NOT_MET", None, None, None, f"{oid} not paid on time"))

    # -- 5. metrics -------------------------------------------------------------------
    focal = net.focal_merchant
    outcomes = []
    for m in net.merchants:
        mine_out = [r for r in reports.values() if r.payer == m]
        mine_in = [r for r in reports.values() if r.payee == m]
        gained = sum(r.extra_concession_cents for r in mine_out)
        given = sum(r.extra_concession_cents for r in mine_in)
        fee_paid = sum(r.payer_fee_cents for r in mine_out) + sum(
            r.payee_fee_cents for r in mine_in
        )
        cost = sum(r.time_cost_cents for r in mine_out)
        value = sum(r.receipt_value_cents for r in mine_in)
        low = min_slack.get(m)
        outcomes.append(
            MerchantOutcome(
                merchant_id=m,
                opening_cents=net.position(m).available.cents,
                closing_cents=usable_cash[m],
                min_slack_cents=low[0] if low else None,
                min_slack_slot=low[1] if low else None,
                discount_gained=gained,
                discount_given=given,
                fees_paid=fee_paid,
                opportunity_cost=cost,
                early_receipt_value=value,
                net_benefit=gained - given - fee_paid - cost + value,
                existing_draw_cents=draw[m],
                hypothetical_need_cents=need_peak[m],
                hypothetical_cents=inject.get(m, 0),
                in_transit_cents=in_transit[m],
            )
        )
    legs = tuple(reports[leg_id] for _, leg_id in accepted)
    face = {e.obligation_id: e.face.cents for e in net.obligations}
    metrics = Metrics(
        on_time_face=sum(face[r.obligation_id] for r in legs if r.on_time),
        on_time_count=sum(1 for r in legs if r.on_time),
        gross_flow=sum(r.amount_cents for r in legs),
        fees=fees,
        early_cash=sum(r.debit_cents for r in legs if r.accelerated),
        cent_seconds=sum(r.debit_cents * r.seconds_early for r in legs),
        surplus=sum(r.receipt_value_cents - r.time_cost_cents for r in legs),
        focal_net=next(o.net_benefit for o in outcomes if o.merchant_id == focal),
        downstream_face=sum(face[r.obligation_id] for r in legs if r.on_time and r.payer != focal),
        in_transit=sum(in_transit.values()),
        external_pending=0,
        required_met=not missing,
        merchants=tuple(outcomes),
        legs=legs,
    )
    return VerificationReport(ok=not bad, violations=tuple(bad), metrics=metrics)
