"""Canonical SYNTHETIC financial scenarios (fixture version `canonical-v1`).

Every object here carries `SourceKind.SYNTHETIC_FIXTURE` provenance or
`EvidenceKind.SYNTHETIC` capacity evidence. None of it is PayPal evidence: real Sandbox
facts live only in `evidence/paypal-sandbox/` and are loaded through
`api.integrations.paypal.settlement`. Downstream legs are SIMULATED and never
provider-confirmed.

Scenario A - normal liquidity cascade (doc 04 canonical illustration)
Scenario B - discount negotiation ($5 offered, buyer needs >= $10)
Scenario C - held / unverified $990 receipt
Scenario D - supplier reserve shortfall ($990 in, $900 out, $150 reserve)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from api.domain.invoice import (
    InvoiceRecord,
    Merchant,
    Obligation,
    ObligationRevision,
    Provenance,
    RevisionStatus,
    SourceKind,
)
from api.domain.money import USD, Money
from api.liquidity.capacity import CapacityEvidence, EvidenceKind, IncomingReceipt, ReceiptStatus
from api.liquidity.simulation import Transfer

FIXTURE_VERSION = "canonical-v1"
AS_OF = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # synthetic clock
TZ = "America/Toronto"
SYN = Provenance(SourceKind.SYNTHETIC_FIXTURE, FIXTURE_VERSION, AS_OF)


def usd(cents: int) -> Money:
    return Money(cents, USD)


MERCHANTS = {
    m: Merchant(m, name, TZ)
    for m, name in (
        ("buyer", "Buyer (synthetic)"),
        ("food_supplier", "Food supplier (synthetic)"),
        ("packaging_supplier", "Packaging supplier (synthetic)"),
        ("carrier", "Carrier (synthetic)"),
        ("mechanic", "Mechanic (synthetic)"),
    )
}


def _original_terms(oid: str, due_days: int) -> ObligationRevision:
    return ObligationRevision(
        obligation_id=oid,
        revision_no=1,
        status=RevisionStatus.AUTHORIZED,
        due_at=AS_OF + timedelta(days=due_days),
        business_tz=TZ,
        concession=usd(0),
        created_at=AS_OF,
        provenance=SYN,
        authorized_by=frozenset(),  # original issued terms; no concession to authorize
    )


def invoice(
    oid: str, debtor: str, creditor: str, face_cents: int, due_days: int = 30
) -> InvoiceRecord:
    ob = Obligation(oid, debtor, creditor, usd(face_cents), AS_OF, SYN)
    return InvoiceRecord.open(ob, _original_terms(oid, due_days))


def early_discount(
    record: InvoiceRecord,
    concession_cents: int,
    *,
    status: RevisionStatus,
    authorized_by: frozenset[str],
    minutes: int,
    expires_minutes: int | None = None,
    supersedes: int | None = None,
) -> InvoiceRecord:
    at = AS_OF + timedelta(minutes=minutes)
    prior = record.revisions[-1]
    return record.add_revision(
        ObligationRevision(
            obligation_id=record.obligation.obligation_id,
            revision_no=prior.revision_no + 1,
            status=status,
            due_at=prior.due_at,
            business_tz=TZ,
            concession=usd(concession_cents),
            created_at=at,
            provenance=SYN,
            expires_at=None if expires_minutes is None else at + timedelta(minutes=expires_minutes),
            authorized_by=authorized_by,
            supersedes=supersedes,
        )
    )


def synthetic_evidence(
    evidence_id: str, merchant: str, spendable: int, reserve: int, hours_valid: int = 4
) -> CapacityEvidence:
    return CapacityEvidence(
        evidence_id=evidence_id,
        merchant_id=merchant,
        verified_spendable=usd(spendable),
        protected_reserve=usd(reserve),
        kind=EvidenceKind.SYNTHETIC,
        observed_at=AS_OF,
        valid_until=AS_OF + timedelta(hours=hours_valid),
        provenance=SYN,
    )


# --- Scenario A ------------------------------------------------------------------------
@dataclass(frozen=True)
class ScenarioA:
    buyer_evidence: CapacityEvidence
    food: InvoiceRecord
    packaging: InvoiceRecord
    freight: InvoiceRecord
    repair: InvoiceRecord
    opening: dict[str, Money]
    reserves: dict[str, Money]
    chain_via_food: tuple[Transfer, ...]
    labels: tuple[str, ...] = ("SYNTHETIC_NETWORK", "DOWNSTREAM_NOT_PROVIDER_VERIFIED")


def scenario_a() -> ScenarioA:
    both = frozenset({"buyer", "food_supplier"})
    food = early_discount(
        invoice("food_001", "buyer", "food_supplier", 100000),
        1000,
        status=RevisionStatus.AUTHORIZED,
        authorized_by=both,
        minutes=5,
    )
    packaging = early_discount(
        invoice("pack_001", "buyer", "packaging_supplier", 100000),
        1000,
        status=RevisionStatus.AUTHORIZED,
        authorized_by=frozenset({"buyer", "packaging_supplier"}),
        minutes=5,
    )
    return ScenarioA(
        buyer_evidence=synthetic_evidence("ev_buyer_a", "buyer", 149000, 50000),
        food=food,
        packaging=packaging,
        freight=invoice("freight_001", "food_supplier", "carrier", 90000),
        repair=invoice("repair_001", "carrier", "mechanic", 80000),
        opening={m: usd(149000 if m == "buyer" else 0) for m in MERCHANTS},
        reserves={"buyer": usd(50000)},
        chain_via_food=(
            Transfer("leg_food", "buyer", "food_supplier", usd(99000)),
            Transfer("leg_freight", "food_supplier", "carrier", usd(90000)),
            Transfer("leg_repair", "carrier", "mechanic", usd(80000)),
        ),
    )


# --- Scenario B ------------------------------------------------------------------------
BUYER_MIN_DISCOUNT = usd(1000)  # buyer policy: at least $10 off for paying early


def scenario_b_supplier_offer() -> InvoiceRecord:
    """Supplier offers $5 (draft signed by supplier only; buyer has not accepted)."""
    return early_discount(
        invoice("food_001", "buyer", "food_supplier", 100000),
        500,
        status=RevisionStatus.DRAFT,
        authorized_by=frozenset({"food_supplier"}),
        minutes=5,
        expires_minutes=60,
    )


def scenario_b_proposed_ten(record: InvoiceRecord) -> InvoiceRecord:
    """A proposed $10 discount (e.g. AI-suggested): still a draft, signed by nobody."""
    return early_discount(
        record,
        1000,
        status=RevisionStatus.DRAFT,
        authorized_by=frozenset(),
        minutes=10,
        expires_minutes=60,
    )


def scenario_b_authorize(record: InvoiceRecord, minutes: int = 20) -> InvoiceRecord:
    draft = record.revisions[-1]
    return early_discount(
        record,
        draft.concession.cents,
        status=RevisionStatus.AUTHORIZED,
        authorized_by=frozenset({"buyer", "food_supplier"}),
        minutes=minutes,
        supersedes=draft.revision_no,
    )


# --- Scenario C ------------------------------------------------------------------------
def scenario_c() -> tuple[CapacityEvidence, IncomingReceipt]:
    ev = synthetic_evidence("ev_food_c", "food_supplier", 0, 15000)
    held = IncomingReceipt(
        "rcpt_food_c",
        "food_supplier",
        usd(99000),
        ReceiptStatus.HELD,
        SYN,
        available_at=None,
    )
    return ev, held


# --- Scenario D ------------------------------------------------------------------------
SUPPLIER_RESERVE = usd(15000)
FREIGHT_DUE = usd(90000)


def scenario_d_fee_free() -> CapacityEvidence:
    """Supplier verified-available $990 (synthetic, zero fees), $150 reserve."""
    return synthetic_evidence("ev_food_d", "food_supplier", 99000, 15000)


def scenario_d_hypothetical_usd_net() -> CapacityEvidence:
    """Hypothetical: the observed USD net ($960.99) were usable USD. Not what happened."""
    return synthetic_evidence("ev_food_d_net", "food_supplier", 96099, 15000)
