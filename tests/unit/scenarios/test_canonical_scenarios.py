"""Canonical scenarios A-E exercised through the real financial rules.

All inputs are SYNTHETIC fixtures (fixtures/canonical.py) except where the real Sandbox
settlement is loaded explicitly from evidence/.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from api.domain.invoice import (
    ConflictingRevisionError,
    DuplicateObligationError,
    ExpiredRevisionError,
    InvalidInvoiceError,
    ObligationRegistry,
    RevisionStatus,
    SettlementEvent,
    SettlementKind,
    SettlementStatus,
)
from api.domain.money import USD, InvalidAmountError, Money, NegativeAmountError
from api.integrations.paypal.settlement import load_settlement
from api.liquidity.capacity import (
    CapacityStatus,
    Commitment,
    InsufficientCapacityError,
    assess_outflow,
    commit_capacity,
    compute_capacity,
)
from api.liquidity.provider_receipts import usd_capacity_effect
from api.liquidity.simulation import ReserveViolationError, Transfer, simulate
from fixtures import canonical as fx

NOW = fx.AS_OF + timedelta(minutes=30)
RUNS = Path(__file__).resolve().parents[3] / "evidence" / "paypal-sandbox" / "runs"


def usd(c):
    return Money(c, USD)


# --- Scenario A: normal cascade ---------------------------------------------------------


def test_a_buyer_can_deploy_exactly_990():
    a = fx.scenario_a()
    s = compute_capacity(a.buyer_evidence, at=NOW, allow_synthetic=True)
    assert s.deployable == usd(99000)


def test_a_only_one_of_the_two_990_invoices_can_be_funded():
    a = fx.scenario_a()
    first = commit_capacity(
        a.buyer_evidence,
        [],
        Commitment("c_food", "buyer", "plan_food", usd(99000)),
        at=NOW,
        allow_synthetic=True,
    )
    with pytest.raises(InsufficientCapacityError):
        commit_capacity(
            a.buyer_evidence,
            first,
            Commitment("c_pack", "buyer", "plan_pack", usd(99000)),
            at=NOW,
            allow_synthetic=True,
        )


def test_a_990_plus_authorized_10_settles_the_1000_invoice():
    a = fx.scenario_a()
    paid = a.food.apply(
        SettlementEvent(
            "pay_food", "food_001", SettlementKind.CASH_APPLIED, usd(99000), NOW, fx.SYN
        )
    )
    p = paid.position()
    assert p.status is SettlementStatus.SETTLED
    assert (p.original_open, p.cash_applied, p.authorized_concession) == (
        usd(100000),
        usd(99000),
        usd(1000),
    )


def test_a_fee_free_chain_conserves_cash_doc04_figures():
    a = fx.scenario_a()
    r = simulate(a.opening, a.reserves, a.chain_via_food)
    assert r.closing["buyer"] == usd(50000)
    assert r.closing["food_supplier"] == usd(9000)
    assert r.closing["carrier"] == usd(10000)
    assert r.closing["mechanic"] == usd(80000)
    assert Money.total(r.closing.values(), USD) == usd(149000)  # conserved
    assert r.gross_flow == usd(269000)  # distinct transfers, not created money
    assert r.label == "SYNTHETIC"
    downstream_face = a.freight.obligation.face + a.repair.obligation.face
    assert downstream_face == usd(170000)


def test_a_downstream_legs_are_labeled_unverified():
    assert "DOWNSTREAM_NOT_PROVIDER_VERIFIED" in fx.scenario_a().labels


def test_a_buyer_reserve_breach_is_refused():
    a = fx.scenario_a()
    greedy = (Transfer("leg_food", "buyer", "food_supplier", usd(99001)),)
    with pytest.raises(ReserveViolationError, match=r"short by USD 0.01"):
        simulate(a.opening, a.reserves, greedy)


def test_a_paying_both_invoices_breaches_reserve():
    a = fx.scenario_a()
    both = (
        Transfer("leg_food", "buyer", "food_supplier", usd(99000)),
        Transfer("leg_pack", "buyer", "packaging_supplier", usd(99000)),
    )
    with pytest.raises(ReserveViolationError):
        simulate(a.opening, a.reserves, both)


# --- Scenario B: negotiation ------------------------------------------------------------


def test_b_five_dollar_offer_is_below_buyer_minimum_and_does_not_count():
    offer = fx.scenario_b_supplier_offer()
    draft = offer.revisions[-1]
    assert draft.status is RevisionStatus.DRAFT
    assert draft.concession < fx.BUYER_MIN_DISCOUNT  # unacceptable to buyer
    assert offer.position().authorized_concession == usd(0)


def test_b_proposed_ten_is_a_draft_until_both_authorize():
    proposed = fx.scenario_b_proposed_ten(fx.scenario_b_supplier_offer())
    assert proposed.revisions[-1].concession >= fx.BUYER_MIN_DISCOUNT
    assert proposed.position().authorized_concession == usd(0)  # still draft
    # Paying $990 against the draft leaves $10 open: the discount is not assumed.
    early = proposed.apply(
        SettlementEvent(
            "p",
            "food_001",
            SettlementKind.CASH_APPLIED,
            usd(99000),
            fx.AS_OF + timedelta(minutes=15),
            fx.SYN,
        )
    )
    assert early.position().remaining_open == usd(1000)


def test_b_authorized_ten_settles_with_990():
    agreed = fx.scenario_b_authorize(fx.scenario_b_proposed_ten(fx.scenario_b_supplier_offer()))
    assert agreed.position().authorized_concession == usd(1000)
    paid = agreed.apply(
        SettlementEvent(
            "p",
            "food_001",
            SettlementKind.CASH_APPLIED,
            usd(99000),
            fx.AS_OF + timedelta(minutes=25),
            fx.SYN,
        )
    )
    assert paid.position().status is SettlementStatus.SETTLED


def test_b_expired_proposal_cannot_be_authorized():
    proposed = fx.scenario_b_proposed_ten(fx.scenario_b_supplier_offer())
    with pytest.raises(ExpiredRevisionError):
        fx.scenario_b_authorize(proposed, minutes=10 + 60)


# --- Scenario C: held receipt -----------------------------------------------------------


def test_c_held_990_adds_zero_spendable():
    ev, held = fx.scenario_c()
    s = compute_capacity(ev, receipts=[held], at=NOW, allow_synthetic=True)
    assert s.deployable == usd(0)
    assert s.verified_incoming == usd(0)
    assert s.status is CapacityStatus.DEFICIT  # $0 spendable vs $150 reserve
    assert s.projected[0].amount == usd(99000)
    assert not assess_outflow(s, fx.FREIGHT_DUE).feasible


# --- Scenario D: reserve shortfall ------------------------------------------------------


def test_d_fee_free_supplier_deployable_840_short_60():
    s = compute_capacity(fx.scenario_d_fee_free(), at=NOW, allow_synthetic=True)
    assert s.deployable == usd(84000)
    a = assess_outflow(s, fx.FREIGHT_DUE)
    assert not a.feasible
    assert a.shortfall == usd(6000)
    assert a.explanation == (
        "needs USD 900.00 + USD 150.00 reserve, has USD 990.00 verified available; "
        "short by USD 60.00"
    )


def test_d_hypothetical_usd_net_after_real_fee_short_89_01():
    s = compute_capacity(fx.scenario_d_hypothetical_usd_net(), at=NOW, allow_synthetic=True)
    assert s.deployable == usd(81099)
    assert assess_outflow(s, fx.FREIGHT_DUE).shortfall == usd(8901)


def test_d_actual_paypal_settlement_is_currency_blocked():
    real = load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")
    assert real.provider_fee == usd(2901)
    assert real.net == usd(96099)  # the hypothetical above uses this figure
    assert usd_capacity_effect(real).usd_deployable == usd(0)


def test_d_synthetic_chain_with_supplier_reserve_fails_at_the_freight_leg():
    a = fx.scenario_a()
    reserves = {**a.reserves, "food_supplier": fx.SUPPLIER_RESERVE}
    with pytest.raises(ReserveViolationError, match=r"short by USD 60.00"):
        simulate(a.opening, reserves, a.chain_via_food)


# --- Scenario E: duplicate and malformed obligations -----------------------------------


def test_e_duplicate_original_invoice_counted_once():
    reg = ObligationRegistry()
    a = fx.scenario_a()
    reg.register(a.food.obligation)
    reg.register(a.food.obligation)
    reg.register(a.packaging.obligation)
    assert len(reg) == 2
    assert reg.total_face(USD) == usd(200000)


def test_e_same_id_with_different_face_rejected():
    reg = ObligationRegistry()
    reg.register(fx.invoice("food_001", "buyer", "food_supplier", 100000).obligation)
    with pytest.raises(DuplicateObligationError):
        reg.register(fx.invoice("food_001", "buyer", "food_supplier", 99000).obligation)


def test_e_conflicting_revisions_rejected():
    a = fx.scenario_a()
    conflicting = a.food.revisions[1].__class__(
        **{
            **{f: getattr(a.food.revisions[1], f) for f in a.food.revisions[1].__slots__},
            "concession": usd(500),
        }
    )
    with pytest.raises(ConflictingRevisionError):
        a.food.add_revision(conflicting)


def test_e_stale_evidence_cannot_fund_anything():
    a = fx.scenario_a()
    late = fx.AS_OF + timedelta(hours=5)
    s = compute_capacity(a.buyer_evidence, at=late, allow_synthetic=True)
    assert s.status is CapacityStatus.STALE
    assert s.deployable == usd(0)


@pytest.mark.parametrize(
    ("build", "error"),
    [
        (lambda: fx.invoice("food_x", "buyer", "food_supplier", 0), InvalidInvoiceError),
        (lambda: fx.invoice("food_x", "buyer", "buyer", 100), InvalidInvoiceError),
        (lambda: fx.usd(-1), NegativeAmountError),
        (lambda: Money(99.0, USD), InvalidAmountError),
    ],
)
def test_e_invalid_amounts_and_parties_rejected(build, error):
    with pytest.raises(error):
        build()
