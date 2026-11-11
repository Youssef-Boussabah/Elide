"""Bounded counteroffer drafting (doc 07 capability B, doc 13 "stronger AI demo").

1. Deterministic diagnosis with the P2 solver: is the requester's invoice payable under
   current terms? If not, why (in requester-visible reason codes)?
2. Deterministic bounds: the smallest concession that satisfies the requester's own
   minimum discount AND fits its deployable cash, and the largest it may ask (its own cap
   and the counterparty's explicitly DISCLOSED maximum, if any). No range -> no model call.
3. The model drafts one proposal + message inside the bounds, from requester-visible facts
   only (never the requester's reserve/balance/hurdle, never the counterparty's private
   rules, which the model does not receive).
4. Deterministic checks: amount exact and in range; message free of private figures,
   invented amounts, acceptance claims and availability claims.
5. The proposal becomes a DRAFT revision (signed by nobody, counteroffering the open offer)
   on a copy of the invoice record; P2 re-plans with it as a negotiated proposal that is
   ASSUMED approved by both parties (simulation). In-force terms never change.
6. Views: the requester sees its own economics and "counterparty approval required"; the
   counterparty's private rules are evaluated separately and shown only in its own view.
No approval evidence is ever created.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from api.ai.adapter import LLMAdapter, LLMOutputError, LLMUnavailableError, ModelCall
from api.ai.prompts import COUNTEROFFER_PROMPT_VERSION, COUNTEROFFER_SYSTEM
from api.ai.schemas import CounterofferProposal
from api.domain.invoice import InvoiceRecord, ObligationRevision, RevisionStatus
from api.domain.money import USD, Money
from api.negotiation.guards import TextIssue, check_text
from api.planning.builder import InvoiceInput, build_network
from api.planning.network import Network, TermsKind
from api.planning.solver import ScheduledLeg, SolveConfig, solve
from api.planning.verifier import verify
from api.policy.validation import _cents

REASON_CODES = (
    "OFFER_BELOW_REQUESTER_MINIMUM",
    "PAYMENT_EXCEEDS_REQUESTER_CAPACITY",
    "NO_EARLY_PAYMENT_OFFER",
)


@dataclass(frozen=True, slots=True)
class NegotiationCase:
    """Everything the engine knows; only parts of it ever reach the model."""

    network_id: str
    as_of: datetime
    records: tuple[InvoiceInput, ...]  # all invoices of the (synthetic) network
    build: dict[str, Any]  # build_network kwargs except invoices/as_of
    invoice_id: str
    requester: str
    counterparty: str
    requester_max_concession: Money  # the requester's own cap on what it asks for
    disclosed_max_concession: Money | None = None  # counterparty's explicit public range
    offer_minutes: int = 60


@dataclass
class CounterofferResult:
    status: str
    invoice_id: str
    reason_codes: list[str] = field(default_factory=list)
    allowed_range: tuple[int, int] | None = None
    proposal: CounterofferProposal | None = None
    model_call: ModelCall | None = None
    message_issues: list[TextIssue] = field(default_factory=list)
    draft_revision: dict[str, Any] | None = None
    record_after: InvoiceRecord | None = None
    requester_view: dict[str, Any] = field(default_factory=dict)
    counterparty_view: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def _usd(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _network(case: NegotiationCase, records: tuple[InvoiceInput, ...]) -> Network:
    return build_network(case.network_id, as_of=case.as_of, invoices=records, **case.build)


def _target(case: NegotiationCase) -> InvoiceInput:
    return next(r for r in case.records if r.record.obligation.obligation_id == case.invoice_id)


def _neutral_counterparty(net: Network, merchant: str) -> Network:
    """The requester-view network: the counterparty's private payee rules are not used."""
    pol = replace(
        net.policy(merchant),
        min_receipt_net_benefit_cents=None,
        max_concession_given=None,
        receipt_cost_cents=0,
    )
    return replace(
        net, policies=tuple(pol if p.merchant_id == merchant else p for p in net.policies)
    )


def propose_counteroffer(adapter: LLMAdapter, case: NegotiationCase) -> CounterofferResult:
    net = _network(case, case.records)
    view_net = _neutral_counterparty(net, case.counterparty)
    edge = net.obligation(case.invoice_id)
    res = CounterofferResult("NO_CHANGE_NEEDED", case.invoice_id)

    # 1. diagnosis (requester view)
    b4 = solve(view_net, SolveConfig())
    if any(view_net.option(x.option_id).obligation_id == case.invoice_id for x in b4.schedule):
        res.note = "the invoice is already payable under current terms"
        return res
    pos, pol = net.position(case.requester), net.policy(case.requester)
    offers = [o for o in net.options_for(case.invoice_id) if o.terms is TermsKind.OPEN_OFFER]
    best_offer = max(
        (o.concession.cents - edge.in_force_concession.cents for o in offers), default=0
    )
    if not offers:
        res.reason_codes.append("NO_EARLY_PAYMENT_OFFER")
    elif best_offer < pol.min_early_discount.cents:
        res.reason_codes.append("OFFER_BELOW_REQUESTER_MINIMUM")
    slack = pos.base_slack_cents
    if edge.open.cents - best_offer > slack:
        res.reason_codes.append("PAYMENT_EXCEEDS_REQUESTER_CAPACITY")

    # 2. bounds (deterministic)
    lo = max(pol.min_early_discount.cents, edge.open.cents - slack, best_offer + 1, 1)
    hi = case.requester_max_concession.cents
    if case.disclosed_max_concession is not None:
        hi = min(hi, case.disclosed_max_concession.cents)
    hi = min(hi, edge.open.cents - 1)
    if lo > hi:
        res.status = "NO_BOUNDED_PROPOSAL"
        res.note = "no concession within the allowed limits makes this invoice work"
        return res
    res.allowed_range = (lo, hi)

    # 3. model draft from requester-visible facts only
    current = f"{_usd(best_offer)}" if offers else "none"
    facts = (
        "{"
        f'"invoice_id": "{case.invoice_id}", "currency": "USD", '
        f'"invoice_face": "{_usd(edge.face.cents)}", '
        f'"amount_still_open": "{_usd(edge.open.cents)}", '
        f'"counterparty_current_offer_discount": "{current}", '
        f'"allowed_discount_range": ["{_usd(lo)}", "{_usd(hi)}"], '
        f'"reason_codes": {list(res.reason_codes)!r}'
        "}"
    ).replace("'", '"')
    try:
        out = adapter.generate_structured(
            system=COUNTEROFFER_SYSTEM,
            user=f"<facts>\n{facts}\n</facts>",
            schema=CounterofferProposal,
            schema_name="counteroffer",
            prompt_version=COUNTEROFFER_PROMPT_VERSION,
            max_tokens=800,
        )
    except LLMOutputError as exc:
        res.status, res.note = "MODEL_OUTPUT_INVALID", str(exc)
        return res
    except LLMUnavailableError as exc:
        res.status, res.note = "MODEL_UNAVAILABLE", str(exc)
        return res
    p = out.value
    res.proposal, res.model_call = p, out.call

    # 4. deterministic checks
    cents = _cents(p.proposed_concession)
    problems = []
    if p.invoice_id != case.invoice_id:
        problems.append("WRONG_INVOICE")
    if cents is None:
        problems.append("INVALID_AMOUNT")
    elif not lo <= cents <= hi:
        problems.append("OUTSIDE_ALLOWED_RANGE")
    payment = edge.open.cents - (cents or 0)
    allowed = {edge.face.cents, edge.open.cents, best_offer, cents or 0, payment}
    private = {
        pos.available.cents,
        pos.protected_reserve.cents,
        slack,
        pol.min_early_discount.cents,
    }
    private_strings = (
        (f"{pol.hurdle_bps / 100:g}%", f"{pol.hurdle_bps} bps") if pol.hurdle_bps else ()
    )
    cp = net.policy(case.counterparty)
    for v in (cp.min_receipt_net_benefit_cents, cp.receipt_cost_cents):
        if v:
            private.add(v)
    if cp.max_concession_given is not None:
        private.add(cp.max_concession_given.cents)
    res.message_issues = check_text(
        p.message_to_counterparty,
        allowed_amounts=allowed,
        private_amounts=private,
        private_strings=private_strings,
    ) + [
        TextIssue(f"RATIONALE_{i.code}", i.detail)
        for i in check_text(
            p.rationale_for_requester,
            allowed_amounts=allowed | {lo, hi},
            private_amounts=set(),
        )
        if i.code in ("ACCEPTANCE_CLAIM", "AVAILABILITY_CLAIM", "INVENTED_AMOUNT")
    ]
    if problems or res.message_issues:
        res.status = "MODEL_OUTPUT_REJECTED"
        res.note = "; ".join(problems + [i.code for i in res.message_issues])
        return res

    # 5. DRAFT revision on a copy of the record (nobody signs it), then re-plan
    target = _target(case)
    rec = target.record
    open_offer_nos = [r.revision_no for r in rec.open_offers(case.as_of)]
    prior = rec.revisions[-1]
    draft = ObligationRevision(
        obligation_id=case.invoice_id,
        revision_no=prior.revision_no + 1,
        status=RevisionStatus.DRAFT,
        due_at=rec.in_force_revision(case.as_of).due_at,
        business_tz=prior.business_tz,
        concession=Money(cents, USD),
        created_at=case.as_of,
        provenance=prior.provenance,
        expires_at=case.as_of + timedelta(minutes=case.offer_minutes),
        authorized_by=frozenset(),
        supersedes=max(open_offer_nos) if open_offer_nos else None,
    )
    rec2 = rec.add_revision(draft)
    if rec2.in_force_revision(case.as_of) != rec.in_force_revision(case.as_of):
        raise RuntimeError("a draft must never change the terms in force")
    records2 = tuple(replace(r, record=rec2) if r is target else r for r in case.records)
    net2 = _network(case, records2)
    view2 = _neutral_counterparty(net2, case.counterparty)
    b5 = solve(view2, SolveConfig(allow_proposals=True))
    rep = verify(view2, b5.schedule, allow_proposals=True)
    prop_id = f"{case.invoice_id}:r{draft.revision_no}:proposal"
    mine = next((r for r in rep.metrics.legs if r.leg_id == prop_id), None)
    res.record_after = rec2
    res.draft_revision = {
        "obligation_id": case.invoice_id,
        "revision_no": draft.revision_no,
        "status": "draft",
        "concession_cents": cents,
        "payment_cents": payment,
        "currency": USD,
        "authorized_by": [],
        "supersedes": draft.supersedes,
        "expires_at": draft.expires_at.isoformat() if draft.expires_at else None,
        "approval_evidence": None,
        "required_approvals": [
            {"merchant_id": case.requester, "action": "sign_terms"},
            {"merchant_id": case.counterparty, "action": "sign_terms"},
            {"merchant_id": case.requester, "action": "authorize_payment"},
        ],
    }
    buyer = rep.metrics.merchant(case.requester)
    res.requester_view = {
        "proposed_discount_cents": cents,
        "proposed_payment_cents": payment,
        "plan_includes_invoice": mine is not None,
        "on_time": bool(mine and mine.on_time),
        "your_modeled_net_benefit_cents": buyer.net_benefit if mine else None,
        "downstream_legs_enabled": sum(
            1 for r in rep.metrics.legs if case.requester not in (r.payer, r.payee)
        ),
        "counterparty": "approval required (its private rules are not shown to you)",
        "status": "SIMULATED: assumes both merchants sign; nothing is agreed or paid",
    }
    # counterparty's own view: its declared rules on this exact leg (private to it)
    check = verify(net2, (ScheduledLeg(prop_id, mine.slot if mine else 0),), allow_proposals=True)
    payee = [v for v in check.violations if v.code in ("PAYEE_REJECTS", "PROHIBITED_COUNTERPARTY")]
    res.counterparty_view = {
        "proposed_discount_cents": cents,
        "proposed_payment_cents": payment,
        "meets_your_declared_rules": not payee,
        "your_rule_issues": [v.detail for v in payee],
        "decision": "yours: sign the draft or decline; Elide never signs for you",
    }
    res.status = "DRAFT_PROPOSED" if mine is not None else "DRAFT_NOT_FEASIBLE"
    if mine is None:
        res.note = "even with the proposal assumed signed, the plan cannot include the invoice"
    return res
