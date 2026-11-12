"""SYNTHETIC persisted workspace (`workspace-seed-v1`) with labelled TEST identities.

The doc 13 "stronger AI demo" network, persisted through the real P4 services:
- buyer: USD 1,490.00 spendable, USD 500.00 protected (synthetic evidence valid 7 days,
  a labelled simulation assumption so an agreed pay-by date inside the week is fundable);
  confirmed policy "keep $500; pay early only for at least $10 off" (validated by the
  P3 validator and confirmed through `policies.confirm`), planning assumption hurdle
  800 bps (labelled synthetic);
- food_001 buyer -> food_supplier USD 1,000.00 due in 30 days; the supplier's open
  offer is USD 5.00 off (made through `negotiation.propose`, so it is signed by the
  supplier only);
- freight_001 food_supplier -> carrier USD 900.00 and repair_001 carrier -> mechanic
  USD 800.00, both due in 30 days; downstream merchants hold no cash.

Every user is `is_test_identity = true`. These are not real merchants and not evidence of
real merchant authentication. Seeding is refused outside local/test/ci.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, insert

from api.ai.schemas import MoneyMention, PolicyExtraction
from api.app.auth import service as auth
from api.domain.invoice import (
    InvoiceRecord,
    Obligation,
    ObligationRevision,
    Provenance,
    RevisionStatus,
    SourceKind,
)
from api.domain.money import USD, Money
from api.liquidity.capacity import CapacityEvidence, EvidenceKind
from api.liquidity.store import save_evidence
from api.policy.service import PolicyDraft
from api.policy.validation import MerchantContext, Status, validate_policy
from api.workflow import negotiation, policies
from api.workflow.store import save_record
from api.workflow.tables import network, network_participation

SEED_VERSION = "workspace-seed-v1"
NETWORK_ID = "synthetic_canonical_network"
TZ = "America/Toronto"
MERCHANTS = {
    "buyer": "Northside Caterer (synthetic)",
    "food_supplier": "Food Supplier (synthetic)",
    "carrier": "Carrier (synthetic)",
    "mechanic": "Mechanic (synthetic)",
}
# login -> (display name, merchant, role)
USERS = {
    "buyer-owner@elide.test": ("Buyer owner (TEST)", "buyer", "owner"),
    "buyer-viewer@elide.test": ("Buyer viewer (TEST)", "buyer", "viewer"),
    "supplier-owner@elide.test": ("Supplier owner (TEST)", "food_supplier", "owner"),
    "supplier-auditor@elide.test": ("Supplier auditor (TEST)", "food_supplier", "auditor"),
    "carrier-owner@elide.test": ("Carrier owner (TEST)", "carrier", "owner"),
    "mechanic-owner@elide.test": ("Mechanic owner (TEST)", "mechanic", "owner"),
}
INVOICES = (
    ("food_001", "buyer", "food_supplier", 100000),
    ("freight_001", "food_supplier", "carrier", 90000),
    ("repair_001", "carrier", "mechanic", 80000),
)
EVIDENCE_VALIDITY = timedelta(days=7)
BUYER_POLICY_TEXT = "Keep $500 protected at all times. Only pay early for at least $10 off."


@dataclass(frozen=True)
class SeedResult:
    users: dict[str, str]  # login -> user_id
    passwords: dict[str, str]  # login -> password (TEST identities; never logged)
    supplier_offer_id: str


def _prov(at: datetime, ref: str) -> Provenance:
    return Provenance(SourceKind.SYNTHETIC_FIXTURE, f"{SEED_VERSION}:{ref}", at)


def evidence(merchant: str, spendable: int, reserve: int, at: datetime) -> CapacityEvidence:
    return CapacityEvidence(
        evidence_id=f"ev_{merchant}_{int(at.timestamp())}",
        merchant_id=merchant,
        verified_spendable=Money(spendable, USD),
        protected_reserve=Money(reserve, USD),
        kind=EvidenceKind.SYNTHETIC,
        observed_at=at,
        valid_until=at + EVIDENCE_VALIDITY,
        provenance=_prov(at, "evidence"),
    )


def refresh_evidence(engine: Engine, at: datetime) -> None:
    """New synthetic observations (the previous ones expire after EVIDENCE_VALIDITY)."""
    for m in MERCHANTS:
        spend, reserve = (149000, 50000) if m == "buyer" else (0, 0)
        save_evidence(engine, evidence(m, spend, reserve, at))


def _buyer_policy_draft(at: datetime) -> PolicyDraft:
    text = BUYER_POLICY_TEXT
    extraction = PolicyExtraction(
        reserve_floor=MoneyMention(amount="500", currency="$", quote="Keep $500 protected"),
        max_early_commitment=None,
        min_early_discount=MoneyMention(amount="10", currency="$", quote="at least $10 off"),
        min_net_benefit=None,
        fee_ceiling=None,
        allowed_counterparties=[],
        prohibited_counterparties=[],
        policy_valid_until=None,
        offer_response_deadline=None,
        participation="unspecified",
        approval_preference="unspecified",
        ambiguities=[],
        refused_requests=[],
        summary="Keep a reserve and require a minimum early-payment discount.",
    )
    report = validate_policy(
        merchant_id="buyer",
        text=text,
        source="merchant_message",
        as_of=at,
        extraction=extraction,
        context=MerchantContext("buyer", TZ, {"food supplier": "food_supplier"}),
    )
    if report.status is not Status.READY_FOR_CONFIRMATION:
        raise RuntimeError(f"seed policy did not validate: {report.status}")
    return PolicyDraft(
        "seed", "buyer", "merchant_message", text, at, "VALIDATED", extraction, None, report
    )


def seed(
    engine: Engine,
    *,
    now: datetime,
    passwords: dict[str, str] | None = None,
    allowed: bool = True,
) -> SeedResult:
    if not allowed:
        raise RuntimeError("demo identities may only be seeded in local, test or ci")
    passwords = passwords or {login: secrets.token_urlsafe(18) for login in USERS}
    t0 = now - timedelta(minutes=30)
    users: dict[str, str] = {}
    with engine.begin() as conn:
        for m, name in MERCHANTS.items():
            auth.create_merchant(conn, m, name, TZ, is_synthetic=True)
        for login, (name, m, role) in USERS.items():
            uid = auth.create_user(conn, login, name, passwords[login], is_test_identity=True)
            users[login] = uid
            auth.grant(conn, uid, m, role, at=t0)
        conn.execute(
            insert(network).values(
                network_id=NETWORK_ID,
                display_name="Canonical synthetic network",
                is_synthetic=True,
                label=f"SYNTHETIC_NETWORK FIXTURE:{SEED_VERSION}",
            )
        )
        for m in MERCHANTS:
            conn.execute(
                insert(network_participation).values(
                    network_id=NETWORK_ID, merchant_id=m, joined_at=t0, share_aggregates=True
                )
            )
        for oid, debtor, creditor, face in INVOICES:
            ob = Obligation(oid, debtor, creditor, Money(face, USD), t0, _prov(t0, oid))
            rev = ObligationRevision(
                obligation_id=oid,
                revision_no=1,
                status=RevisionStatus.AUTHORIZED,
                due_at=t0 + timedelta(days=30),
                business_tz=TZ,
                concession=Money(0, USD),
                created_at=t0,
                provenance=_prov(t0, oid),
            )
            save_record(conn, NETWORK_ID, InvoiceRecord.open(ob, rev))
        buyer = users["buyer-owner@elide.test"]
        draft_id = policies.save_draft(
            conn, _buyer_policy_draft(t0), user_id=buyer, session_id="seed", at=t0
        )
        report = _buyer_policy_draft(t0).report
        policies.confirm(
            conn,
            merchant_id="buyer",
            draft_id=draft_id,
            digest=report.digest,
            user_id=buyer,
            session_id="seed",
            at=t0,
            planning_assumptions={"hurdle_bps": 800, "source": f"synthetic ({SEED_VERSION})"},
        )
        offer = negotiation.propose(
            conn,
            merchant_id="food_supplier",
            user_id=users["supplier-owner@elide.test"],
            session_id="seed",
            obligation_id="food_001",
            concession_cents=500,
            pay_by=t0 + timedelta(days=3),
            expires_at=t0 + timedelta(days=3),
            at=t0 + timedelta(minutes=5),
            message="Pay food_001 early for $5.00 off.",
            operation_key="seed:supplier-offer",
        )
    refresh_evidence(engine, t0 + timedelta(minutes=10))
    return SeedResult(users, passwords, offer["offer_id"])
