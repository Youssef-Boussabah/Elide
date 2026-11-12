"""Sandbox pilot network (`sandbox-pilot-v1`): the smallest NON-synthetic P5 scenario.

Two merchants with labelled TEST identities, intended to be bound to PayPal **Sandbox**
accounts (no real money):
- `pilot_seller` (payee): would be bound, by the owner, to the Sandbox Business account
  that owns the app credentials (the invoicer);
- `pilot_buyer` (payer): would be bound to the Sandbox Personal account that pays in
  PayPal's hosted page.

`pilot_001` (and `pilot_002`... with `obligations`): pilot_buyer owes pilot_seller
`face_cents` (default USD 25.00), due in 10 days, merchant-entered. The seller's open
offer on each: `concession_cents` off (default USD 1.00) if paid within 3 days (signed by
the seller only). The buyer's confirmed policy keeps USD 50.00 protected.

What this seed does NOT do (each is a separate, explicit human step):
- bind PayPal accounts (`scripts/bind_sandbox_accounts.py`, owner action);
- attest the buyer's capacity (the buyer attests its own balance in the workspace), unless
  `attest_buyer` is passed (tests only);
- accept the offer, approve the payment, create an intent, or call PayPal.

The network is not synthetic: intents created here CAN execute once bindings exist and
`PAYMENTS_EXECUTION_ENABLED=true`. Seeding is refused outside local/test/ci.
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
from api.execution import views
from api.policy.service import PolicyDraft
from api.policy.validation import MerchantContext, Status, validate_policy
from api.workflow import negotiation, policies
from api.workflow.store import save_record
from api.workflow.tables import network, network_participation

SEED_VERSION = "sandbox-pilot-v1"
NETWORK_ID = "sandbox_pilot_network"
OBLIGATION_ID = "pilot_001"
TZ = "America/Toronto"
MERCHANTS = {
    "pilot_buyer": "Pilot Buyer (Sandbox TEST identity)",
    "pilot_seller": "Pilot Seller (Sandbox TEST identity)",
}
USERS = {
    "pilot-buyer@elide.test": ("Pilot buyer owner (TEST)", "pilot_buyer", "owner"),
    "pilot-seller@elide.test": ("Pilot seller owner (TEST)", "pilot_seller", "owner"),
}
POLICY_TEXT = "Keep $50 protected at all times. Only pay early for at least $1 off."


@dataclass(frozen=True, slots=True)
class PilotSeed:
    users: dict[str, str]
    passwords: dict[str, str]
    offer_id: str


def _prov(at: datetime, ref: str) -> Provenance:
    return Provenance(SourceKind.MERCHANT_ENTERED, f"{SEED_VERSION}:{ref}", at)


def _policy(at: datetime) -> PolicyDraft:
    extraction = PolicyExtraction(
        reserve_floor=MoneyMention(amount="50", currency="$", quote="Keep $50 protected"),
        max_early_commitment=None,
        min_early_discount=MoneyMention(amount="1", currency="$", quote="at least $1 off"),
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
        merchant_id="pilot_buyer",
        text=POLICY_TEXT,
        source="merchant_message",
        as_of=at,
        extraction=extraction,
        context=MerchantContext("pilot_buyer", TZ, {"pilot seller": "pilot_seller"}),
    )
    if report.status is not Status.READY_FOR_CONFIRMATION:
        raise RuntimeError(f"pilot policy did not validate: {report.status}")
    return PolicyDraft(
        "seed",
        "pilot_buyer",
        "merchant_message",
        POLICY_TEXT,
        at,
        "VALIDATED",
        extraction,
        None,
        report,
    )


def seed_pilot(
    engine: Engine,
    *,
    now: datetime,
    passwords: dict[str, str] | None = None,
    allowed: bool = True,
    face_cents: int = 2500,
    concession_cents: int = 100,
    attest_buyer: tuple[int, int] | None = None,
    obligations: int = 1,
) -> PilotSeed:
    if not allowed:
        raise RuntimeError("pilot identities may only be seeded in local, test or ci")
    if not 0 < concession_cents < face_cents:
        raise ValueError("the concession must be positive and below the face amount")
    if not 1 <= obligations <= 9:
        raise ValueError("1 to 9 pilot obligations")
    obligation_ids = [f"pilot_{n:03d}" for n in range(1, obligations + 1)]
    passwords = passwords or {login: secrets.token_urlsafe(18) for login in USERS}
    t0 = now - timedelta(minutes=30)
    users: dict[str, str] = {}
    with engine.begin() as conn:
        for m, name in MERCHANTS.items():
            auth.create_merchant(conn, m, name, TZ, is_synthetic=False)
        for login, (name, m, role) in USERS.items():
            uid = auth.create_user(conn, login, name, passwords[login], is_test_identity=True)
            users[login] = uid
            auth.grant(conn, uid, m, role, at=t0)
        conn.execute(
            insert(network).values(
                network_id=NETWORK_ID,
                display_name="Sandbox pilot (TEST identities, PayPal Sandbox, no real money)",
                is_synthetic=False,
                label=f"SANDBOX_PILOT FIXTURE:{SEED_VERSION}",
            )
        )
        for m in MERCHANTS:
            conn.execute(
                insert(network_participation).values(
                    network_id=NETWORK_ID, merchant_id=m, joined_at=t0, share_aggregates=True
                )
            )
        for oid in obligation_ids:
            ob = Obligation(
                oid, "pilot_buyer", "pilot_seller", Money(face_cents, USD), t0, _prov(t0, oid)
            )
            rev = ObligationRevision(
                obligation_id=oid,
                revision_no=1,
                status=RevisionStatus.AUTHORIZED,
                due_at=t0 + timedelta(days=10),
                business_tz=TZ,
                concession=Money(0, USD),
                created_at=t0,
                provenance=_prov(t0, oid),
            )
            save_record(conn, NETWORK_ID, InvoiceRecord.open(ob, rev))
        buyer = users["pilot-buyer@elide.test"]
        draft = _policy(t0)
        draft_id = policies.save_draft(conn, draft, user_id=buyer, session_id="seed", at=t0)
        policies.confirm(
            conn,
            merchant_id="pilot_buyer",
            draft_id=draft_id,
            digest=draft.report.digest,
            user_id=buyer,
            session_id="seed",
            at=t0,
            planning_assumptions={"hurdle_bps": 800, "source": f"assumption ({SEED_VERSION})"},
        )
        offer_ids = []
        for n, oid in enumerate(obligation_ids):
            offer = negotiation.propose(
                conn,
                merchant_id="pilot_seller",
                user_id=users["pilot-seller@elide.test"],
                session_id="seed",
                obligation_id=oid,
                concession_cents=concession_cents,
                pay_by=t0 + timedelta(days=3),
                expires_at=t0 + timedelta(days=2),
                at=t0 + timedelta(minutes=5),
                message=f"Pay {oid} early for ${concession_cents / 100:.2f} off.",
                operation_key=f"seed:pilot-offer-{n}",
            )
            offer_ids.append(offer["offer_id"])
        if attest_buyer is not None:
            spendable, reserve = attest_buyer
            views.attest_capacity(
                conn,
                merchant_id="pilot_buyer",
                user_id=buyer,
                spendable_cents=spendable,
                reserve_cents=reserve,
                valid_hours=72,
                included_receipt_ids=[],
                included_outflow_ids=[],
                at=t0 + timedelta(minutes=10),
            )
    return PilotSeed(users, passwords, offer_ids[0])
