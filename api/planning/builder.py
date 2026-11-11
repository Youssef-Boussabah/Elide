"""Build planning networks from Phase 1 objects.

- `position_from_evidence`: a `CapacityEvidence` (+ commitments, receipts) evaluated by
  `liquidity.capacity.compute_capacity` at `as_of`; one receipt counts once, held /
  provider-confirmed / unverified receipts count for zero, and only receipts with
  availability evidence become future inflows.
- `position_from_settlement`: a PayPal settlement credit is a recorded fact; a non-USD
  credit is currency-blocked (D008) and a provider confirmation is not usable cash (D006).
- `options_from_record`: an `InvoiceRecord` becomes one edge and its candidate legs: the
  in-force terms plus every still-open offer or proposal. Rejected, closed, expired,
  disputed, refused, non-USD or unknown-fee candidates become `Exclusion`s with reasons.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from api.domain.invoice import DisputeStatus, InvoiceRecord, RevisionStatus
from api.domain.money import USD, Money
from api.domain.settlement import ProviderSettlement
from api.liquidity.capacity import (
    AvailabilityBasis,
    CapacityEvidence,
    Commitment,
    IncomingReceipt,
    ReceiptStatus,
    compute_capacity,
)
from api.liquidity.provider_receipts import receipt_from_settlement, usd_capacity_effect
from api.planning.network import (
    NO_FEES,
    Authorization,
    AvailabilityModel,
    ConsentBasis,
    Exclusion,
    FeeStatus,
    FeeTerms,
    MerchantPolicy,
    MerchantPosition,
    Network,
    ObligationEdge,
    PaymentOption,
    ScheduledInflow,
    SlotClock,
    TermsKind,
    UnverifiedFunds,
)

ZERO = Money(0, USD)


def position_from_evidence(
    evidence: CapacityEvidence,
    *,
    as_of: datetime,
    clock: SlotClock,
    commitments: Iterable[Commitment] = (),
    receipts: Iterable[IncomingReceipt] = (),
    allow_synthetic: bool = True,
) -> MerchantPosition:
    receipts = tuple(receipts)
    snap = compute_capacity(
        evidence, commitments, receipts, at=as_of, allow_synthetic=allow_synthetic
    )
    by_id = {r.receipt_id: r for r in receipts}
    inflows: list[ScheduledInflow] = []
    unverified: list[UnverifiedFunds] = []
    for pf in snap.projected:
        r = by_id[pf.receipt_id]
        basis = r.availability.basis if r.availability else None
        uf = r.usable_from
        future_ok = (
            r.status is ReceiptStatus.AVAILABLE_VERIFIED
            and r.amount.currency == USD
            and uf is not None
            and uf >= as_of
            and (allow_synthetic or basis is not AvailabilityBasis.SYNTHETIC)
        )
        slot = clock.first_slot_after(uf) if future_ok and uf is not None else None
        if future_ok and slot is not None and basis is not None:
            inflows.append(ScheduledInflow(r.receipt_id, r.amount, slot, basis.value))
        else:
            reason = pf.reason if not future_ok else "becomes available after the horizon"
            unverified.append(
                UnverifiedFunds(r.receipt_id, r.amount, f"{r.status.value}: {reason}")
            )

    notes = list(snap.reasons)
    blocked: tuple[Money, ...] = ()
    if snap.currency != USD:
        blocked = (evidence.verified_spendable,)
        available, reserve, committed = ZERO, ZERO, ZERO
        notes.append("non-USD reserve not modelled; the balance is currency-blocked (D008)")
    else:
        reserve, committed = snap.protected_reserve, snap.committed
        available = snap.verified_spendable + snap.verified_incoming if snap.usable else ZERO
        if not snap.usable:
            notes.append(f"{snap.status.value}: opening balance counts for zero")
    return MerchantPosition(
        merchant_id=evidence.merchant_id,
        available=available,
        protected_reserve=reserve,
        committed=committed,
        evidence_id=evidence.evidence_id,
        evidence_kind=evidence.kind.value,
        valid_until=evidence.valid_until,
        held=evidence.held if evidence.held and evidence.held.currency == USD else ZERO,
        pending=evidence.pending if evidence.pending and evidence.pending.currency == USD else ZERO,
        inflows=tuple(inflows),
        unverified=tuple(unverified),
        blocked=blocked,
        notes=tuple(notes),
    )


def position_from_settlement(
    settlement: ProviderSettlement, merchant_id: str, *, reserve: Money = ZERO
) -> MerchantPosition:
    """The merchant's only funds are this provider-confirmed settlement credit."""
    receipt = receipt_from_settlement(settlement, merchant_id)
    effect = usd_capacity_effect(settlement)
    credit = settlement.cash_receipt()
    blocked = (credit,) if credit.currency != USD else ()
    unverified = (
        ()
        if blocked
        else (UnverifiedFunds(receipt.receipt_id, credit, "provider_confirmed (D006)"),)
    )
    return MerchantPosition(
        merchant_id=merchant_id,
        available=ZERO,
        protected_reserve=reserve,
        committed=ZERO,
        evidence_id=receipt.receipt_id,
        evidence_kind="provider_confirmed_settlement",
        valid_until=None,
        unverified=unverified,
        blocked=blocked,
        notes=effect.blocked_by,
    )


def synthetic_position(
    merchant_id: str, available_cents: int = 0, reserve_cents: int = 0
) -> MerchantPosition:
    return MerchantPosition(
        merchant_id=merchant_id,
        available=Money(available_cents, USD),
        protected_reserve=Money(reserve_cents, USD),
        committed=ZERO,
        evidence_id=f"syn_{merchant_id}",
        evidence_kind="synthetic",
        valid_until=None,
        notes=(
            "SYNTHETIC position, assumed current through the horizon; must be re-verified "
            "before any execution",
        ),
    )


@dataclass(frozen=True, slots=True)
class InvoiceInput:
    record: InvoiceRecord
    fee: FeeTerms = NO_FEES
    provider_ref: str | None = None


def edge_from_record(
    record: InvoiceRecord, as_of: datetime, provider_ref: str | None = None
) -> ObligationEdge:
    rev = record.in_force_revision(as_of)
    pos = record.position(as_of)
    ob = record.obligation
    return ObligationEdge(
        obligation_id=ob.obligation_id,
        debtor=ob.debtor,
        creditor=ob.creditor,
        face=ob.face,
        cash_applied=pos.cash_applied,
        in_force_concession=rev.concession,
        revision_no=rev.revision_no,
        due_at=rev.due_at,
        provenance=ob.provenance,
        dispute=rev.dispute,
        provider_ref=provider_ref,
    )


def _auths(
    pairs: Iterable[tuple[str, str]], consent: Mapping[str, ConsentBasis]
) -> tuple[Authorization, ...]:
    return tuple(
        Authorization(m, action, consent.get(m, ConsentBasis.SYNTHETIC_ASSUMPTION))
        for m, action in pairs
    )


def options_from_record(
    inp: InvoiceInput,
    *,
    as_of: datetime,
    clock: SlotClock,
    consent: Mapping[str, ConsentBasis] | None = None,
) -> tuple[ObligationEdge, tuple[PaymentOption, ...], tuple[Exclusion, ...]]:
    consent = consent or {}
    record = inp.record
    edge = edge_from_record(record, as_of, inp.provider_ref)
    oid, payer, payee = edge.obligation_id, edge.debtor, edge.creditor
    excl: list[Exclusion] = []
    if edge.open.cents == 0:
        return edge, (), (Exclusion(oid, oid, "ALREADY_SETTLED", "nothing left open"),)
    if edge.dispute is DisputeStatus.OPEN:
        return edge, (), (Exclusion(oid, oid, "DISPUTED", "open dispute: not payable"),)
    if edge.currency != USD:
        why = f"{edge.currency} obligation: USD-only planner (D008)"
        return edge, (), (Exclusion(oid, oid, "CURRENCY_BLOCKED", why),)
    if inp.fee.status is FeeStatus.UNKNOWN:
        why = "fee unknown and unbounded: cannot prove the reserve holds"
        return edge, (), (Exclusion(oid, oid, "FEE_UNKNOWN", why),)

    candidates: list[PaymentOption] = [
        PaymentOption(
            option_id=f"{oid}:r{edge.revision_no}",
            obligation_id=oid,
            revision_no=edge.revision_no,
            terms=TermsKind.IN_FORCE,
            payer=payer,
            payee=payee,
            cash=edge.open,
            concession=edge.in_force_concession,
            fee=inp.fee,
            authorizations=_auths([(payer, "authorize_payment")], consent),
            provenance=record.obligation.provenance,
        )
    ]
    open_nos = {r.revision_no for r in record.open_offers(as_of)}
    closed = {r.supersedes for r in record.revisions if r.supersedes is not None}
    for r in record.revisions:
        if r.status is not RevisionStatus.DRAFT or r.created_at > as_of:
            continue
        offer = payee in r.authorized_by
        kind = TermsKind.OPEN_OFFER if offer else TermsKind.NEGOTIATED_PROPOSAL
        sid = f"{oid}:r{r.revision_no}:{'offer' if offer else 'proposal'}"
        if r.revision_no not in open_nos:
            code = "OFFER_CLOSED" if r.revision_no in closed else "OFFER_EXPIRED"
            excl.append(Exclusion(sid, oid, code, f"revision {r.revision_no} is no longer open"))
            continue
        if r.expires_at is not None and clock.last_slot_before(r.expires_at) is None:
            excl.append(Exclusion(sid, oid, "OFFER_EXPIRED", "no slot before the deadline"))
            continue
        if r.concession < edge.in_force_concession:
            excl.append(Exclusion(sid, oid, "WORSE_THAN_IN_FORCE", "smaller concession"))
            continue
        cash = record.obligation.face - edge.cash_applied - r.concession
        if cash.cents == 0:
            excl.append(Exclusion(sid, oid, "NO_CASH_LEG", "terms leave nothing to pay"))
            continue
        pairs = (
            [(payer, "accept_offer"), (payer, "authorize_payment")]
            if offer
            else [(payer, "sign_terms"), (payee, "sign_terms"), (payer, "authorize_payment")]
        )
        candidates.append(
            PaymentOption(
                option_id=sid,
                obligation_id=oid,
                revision_no=r.revision_no,
                terms=kind,
                payer=payer,
                payee=payee,
                cash=cash,
                concession=r.concession,
                fee=inp.fee,
                authorizations=_auths(pairs, consent),
                provenance=r.provenance,
                expires_at=r.expires_at,
            )
        )
    options: list[PaymentOption] = []
    for o in candidates:
        refused = [a.merchant_id for a in o.authorizations if a.basis is ConsentBasis.REFUSED]
        if refused:
            why = f"declined by {', '.join(sorted(set(refused)))}"
            excl.append(Exclusion(o.option_id, oid, "CONSENT_REFUSED", why))
        else:
            options.append(o)
    return edge, tuple(options), tuple(excl)


def build_network(
    network_id: str,
    *,
    as_of: datetime,
    clock: SlotClock,
    availability: AvailabilityModel,
    focal_merchant: str,
    positions: Iterable[MerchantPosition],
    policies: Iterable[MerchantPolicy],
    invoices: Iterable[InvoiceInput],
    consent: Mapping[str, ConsentBasis] | None = None,
    labels: tuple[str, ...] = ("SYNTHETIC_NETWORK",),
) -> Network:
    policies = tuple(policies)
    by_merchant = {p.merchant_id: p for p in policies}
    edges, options, exclusions = [], [], []
    for inp in invoices:
        edge, opts, excl = options_from_record(inp, as_of=as_of, clock=clock, consent=consent)
        edges.append(edge)
        exclusions.extend(excl)
        if opts and not by_merchant[edge.debtor].participating:
            for o in opts:
                why = f"{edge.debtor} does not participate"
                exclusions.append(
                    Exclusion(o.option_id, edge.obligation_id, "NOT_PARTICIPATING", why)
                )
            continue
        options.extend(opts)
    return Network(
        network_id=network_id,
        as_of=as_of,
        clock=clock,
        availability=availability,
        focal_merchant=focal_merchant,
        positions=tuple(positions),
        policies=policies,
        obligations=tuple(edges),
        options=tuple(options),
        exclusions=tuple(exclusions),
        labels=labels,
    )
