"""Canonical P2 planning networks (fixture version `canonical-p2-v1`), all SYNTHETIC.

Built from the P1 fixtures in `fixtures.canonical` (same merchants, invoices, $1,490 /
$500 buyer evidence, $5 -> $10 negotiation, held receipt, $150 supplier reserve). The
only real fact used is the PayPal Sandbox settlement (USD 990.00 gross, USD 29.01 fee,
CAD 1,306.79 credit), loaded from `evidence/paypal-sandbox/runs` and labelled as such;
its fee is one observation, not a fee model.

Time: daily slots from PLAN_AS_OF (= AS_OF + 30 min, after the fixture revisions), 31
slots, so invoices due AS_OF + 30 days have due slot 29. Downstream merchants'
positions are synthetic and assumed current through the horizon.

Supplier early-payment discounts are OPEN offers (DRAFT signed by the supplier, deadline
60 min after the offer): paying early is conditional on accepting before the deadline.
`fixtures.canonical.scenario_a` keeps the P1 accounting illustration with an already
authorized (unconditional) $10 amendment; the planner needs the conditional form.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from api.domain.invoice import InvoiceRecord, RevisionStatus
from api.domain.money import CAD, USD, Money
from api.integrations.paypal.settlement import load_settlement
from api.planning.builder import (
    InvoiceInput,
    build_network,
    position_from_evidence,
    position_from_settlement,
    synthetic_position,
)
from api.planning.network import (
    NO_FEES,
    SYNTHETIC_ZERO_LAG,
    Authorization,
    ConsentBasis,
    FeeTerms,
    MerchantPolicy,
    Network,
    ObligationEdge,
    PaymentOption,
    SlotClock,
    TermsKind,
)
from api.planning.solver import ScheduledLeg
from fixtures.canonical import (
    AS_OF,
    BUYER_MIN_DISCOUNT,
    SYN,
    early_discount,
    invoice,
    scenario_b_proposed_ten,
    scenario_b_supplier_offer,
    scenario_c,
    scenario_d_fee_free,
    scenario_d_hypothetical_usd_net,
    synthetic_evidence,
)

FIXTURE_VERSION = "canonical-p2-v1"
PLAN_AS_OF = AS_OF + timedelta(minutes=30)
SLOTS = 31
RUNS = Path(__file__).resolve().parents[1] / "evidence" / "paypal-sandbox" / "runs"
SANDBOX_FEE = Money(2901, USD)
FEE_LABEL = (
    "FEE_EXAMPLE: USD 29.01 is one observed PayPal Sandbox fee on a USD 990.00 capture; "
    "not a fee model for other merchants or payments"
)
BASE_LABELS = ("SYNTHETIC_NETWORK", f"FIXTURE:{FIXTURE_VERSION}")


def leg(option_id: str, slot: int) -> ScheduledLeg:
    return ScheduledLeg(option_id, slot)


def clock(as_of=PLAN_AS_OF, slots: int = SLOTS) -> SlotClock:
    return SlotClock.uniform(as_of, timedelta(days=1), slots)


def usd(c: int) -> Money:
    return Money(c, USD)


def buyer_policy(hurdle_bps: int = 800) -> MerchantPolicy:
    return MerchantPolicy("buyer", hurdle_bps=hurdle_bps, min_early_discount=BUYER_MIN_DISCOUNT)


def _policies(merchants, **overrides: MerchantPolicy) -> list[MerchantPolicy]:
    return [overrides.get(m, MerchantPolicy(m)) for m in merchants]


def _offer(oid: str, supplier: str, concession: int) -> InvoiceRecord:
    return early_discount(
        invoice(oid, "buyer", supplier, 100000),
        concession,
        status=RevisionStatus.DRAFT,
        authorized_by=frozenset({supplier}),
        minutes=5,
        expires_minutes=60,
    )


def _downstream() -> list[InvoiceInput]:
    return [
        InvoiceInput(invoice("freight_001", "food_supplier", "carrier", 90000)),
        InvoiceInput(invoice("repair_001", "carrier", "mechanic", 80000)),
    ]


def _buyer_position(as_of, c, spendable=149000, hours=4):
    ev = synthetic_evidence("ev_buyer_a", "buyer", spendable, 50000, hours_valid=hours)
    return position_from_evidence(ev, as_of=as_of, clock=c)


def network_two_candidates(
    buyer_hurdle_bps: int = 800,
    evidence_hours: int = 4,
    pack_concession: int = 1000,
    supplier_policy: MerchantPolicy | None = None,
) -> Network:
    """Two $10-off offers on $1,000 invoices; buyer can afford one ($990 deployable).

    Canonical economics (unchanged since `canonical-p2-v1`): buyer +$3.70 modeled, food
    supplier -$10.00 modeled (it signed the offer but declared no receipt value or
    threshold). This is NOT a win-win; see `network_positive_sum` for one with disclosed
    supplier assumptions.
    """
    c = clock()
    ms = ("buyer", "food_supplier", "packaging_supplier", "carrier", "mechanic")
    overrides = {"buyer": buyer_policy(buyer_hurdle_bps)}
    if supplier_policy is not None:
        overrides[supplier_policy.merchant_id] = supplier_policy
    return build_network(
        "canonical_two_candidates",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[_buyer_position(PLAN_AS_OF, c, hours=evidence_hours)]
        + [synthetic_position(m) for m in ms[1:]],
        policies=_policies(ms, **overrides),
        invoices=[
            InvoiceInput(_offer("food_001", "food_supplier", 1000)),
            InvoiceInput(_offer("pack_001", "packaging_supplier", pack_concession)),
            *_downstream(),
        ],
        labels=BASE_LABELS,
    )


def network_buyer_tradeoff() -> Network:
    """Packaging offers $20 off (better for the buyer) but unlocks nothing downstream."""
    return network_two_candidates(pack_concession=2000)


def network_negotiation(as_of_minutes: int = 30, buyer_deployable_extra: int = 0) -> Network:
    """Scenario B: supplier offers $5 (signed), a $10 proposal is signed by nobody yet."""
    as_of = AS_OF + timedelta(minutes=as_of_minutes)
    c = clock(as_of)
    record = scenario_b_proposed_ten(scenario_b_supplier_offer())
    ms = ("buyer", "food_supplier", "carrier", "mechanic")
    return build_network(
        "canonical_negotiation",
        as_of=as_of,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[_buyer_position(as_of, c, spendable=149000 + buyer_deployable_extra)]
        + [synthetic_position(m) for m in ms[1:]],
        policies=_policies(ms, buyer=buyer_policy()),
        invoices=[InvoiceInput(record), *_downstream()],
        labels=BASE_LABELS,
    )


def network_held_receipt() -> Network:
    """Scenario C: the supplier's $990 is HELD; it cannot fund the $900 freight."""
    c = clock()
    ev, held = scenario_c()
    ms = ("food_supplier", "carrier", "mechanic")
    return build_network(
        "canonical_held_receipt",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="food_supplier",
        positions=[position_from_evidence(ev, as_of=PLAN_AS_OF, clock=c, receipts=[held])]
        + [synthetic_position(m) for m in ms[1:]],
        policies=_policies(ms),
        invoices=_downstream(),
        labels=BASE_LABELS,
    )


def network_reserve_shortfall(hypothetical_usd_net: bool = False) -> Network:
    """Scenario D: $990 (or the hypothetical USD net $960.99) available, $150 reserve."""
    c = clock()
    ev = scenario_d_hypothetical_usd_net() if hypothetical_usd_net else scenario_d_fee_free()
    ms = ("food_supplier", "carrier", "mechanic")
    labels = (*BASE_LABELS, FEE_LABEL) if hypothetical_usd_net else BASE_LABELS
    return build_network(
        "canonical_reserve_shortfall",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="food_supplier",
        positions=[position_from_evidence(ev, as_of=PLAN_AS_OF, clock=c)]
        + [synthetic_position(m) for m in ms[1:]],
        policies=_policies(ms),
        invoices=_downstream(),
        labels=labels,
    )


def network_fee_cascade(payer_pays_fee: bool = False) -> Network:
    """The $990 payment with the Sandbox-sized USD 29.01 fee, supplier reserve $150."""
    c = clock()
    fee = (
        FeeTerms(payer_fee=SANDBOX_FEE, source="paypal_sandbox_observed_example")
        if payer_pays_fee
        else FeeTerms(payee_fee=SANDBOX_FEE, source="paypal_sandbox_observed_example")
    )
    ms = ("buyer", "food_supplier", "carrier", "mechanic")
    return build_network(
        "canonical_fee_cascade",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[
            _buyer_position(PLAN_AS_OF, c),
            synthetic_position("food_supplier", 0, 15000),
            synthetic_position("carrier"),
            synthetic_position("mechanic"),
        ],
        policies=_policies(ms, buyer=buyer_policy()),
        invoices=[InvoiceInput(_offer("food_001", "food_supplier", 1000), fee=fee), *_downstream()],
        labels=(*BASE_LABELS, FEE_LABEL),
    )


def network_cad_settlement() -> Network:
    """The real Sandbox capture settled as CAD 1,306.79: no USD to pay the freight."""
    c = clock()
    settlement = load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")
    ms = ("food_supplier", "carrier", "mechanic")
    return build_network(
        "canonical_cad_settlement",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="food_supplier",
        positions=[position_from_settlement(settlement, "food_supplier")]
        + [synthetic_position(m) for m in ms[1:]],
        policies=_policies(ms),
        invoices=_downstream(),
        labels=(
            *BASE_LABELS,
            "PAYPAL_SANDBOX_FACT: the supplier's credit is the CAD settlement of the Sandbox "
            "capture (evidence/paypal-sandbox/runs); the downstream obligations are synthetic",
        ),
    )


def network_loop() -> Network:
    """Doc 05: A owes B, B owes C, C owes A $100 each; nobody has usable cash."""
    c = clock()
    ms = ("a", "b", "c")
    return build_network(
        "canonical_loop",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="a",
        positions=[synthetic_position(m) for m in ms],
        policies=_policies(ms),
        invoices=[
            InvoiceInput(invoice("ab_001", "a", "b", 10000, due_days=3)),
            InvoiceInput(invoice("bc_001", "b", "c", 10000, due_days=3)),
            InvoiceInput(invoice("ca_001", "c", "a", 10000, due_days=3)),
        ],
        labels=BASE_LABELS,
    )


def network_parallel_invoices() -> Network:
    """Two separate invoices between the same buyer and supplier."""
    c = clock()
    ev = synthetic_evidence("ev_buyer_par", "buyer", 200000, 50000)
    ms = ("buyer", "food_supplier")
    return build_network(
        "canonical_parallel",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[
            position_from_evidence(ev, as_of=PLAN_AS_OF, clock=c),
            synthetic_position("food_supplier"),
        ],
        policies=_policies(ms),
        invoices=[
            InvoiceInput(_offer("food_001", "food_supplier", 1000)),
            InvoiceInput(invoice("food_002", "buyer", "food_supplier", 40000)),
        ],
        labels=BASE_LABELS,
    )


def network_cad_obligation() -> Network:
    """A CAD-denominated obligation inserted directly (the builder would exclude it)."""
    net = network_two_candidates()
    cad = Money(50000, CAD)
    edge = ObligationEdge(
        "cad_001",
        "buyer",
        "food_supplier",
        cad,
        Money(0, CAD),
        Money(0, CAD),
        1,
        AS_OF + timedelta(days=30),
        SYN,
    )
    opt = PaymentOption(
        "cad_001:r1",
        "cad_001",
        1,
        TermsKind.IN_FORCE,
        "buyer",
        "food_supplier",
        cad,
        Money(0, CAD),
        NO_FEES,
        (Authorization("buyer", "authorize_payment", ConsentBasis.SYNTHETIC_ASSUMPTION),),
        SYN,
    )
    return replace(net, obligations=(*net.obligations, edge), options=(*net.options, opt))


def network_ten_legs() -> Network:
    """Ten candidate legs over six merchants and five daily slots (solver size target)."""
    c = clock(slots=5)
    ms = ("m0", "m1", "m2", "m3", "m4", "m5")
    pairs = [
        ("o01", "m0", "m1", 100000),
        ("o02", "m0", "m2", 80000),
        ("o03", "m1", "m3", 70000),
        ("o04", "m2", "m3", 50000),
        ("o05", "m3", "m4", 90000),
        ("o06", "m4", "m5", 60000),
        ("o07", "m1", "m5", 20000),
        ("o08", "m5", "m0", 30000),
        ("o09", "m2", "m4", 25000),
        ("o10", "m3", "m5", 15000),
    ]
    return build_network(
        "canonical_ten_legs",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="m0",
        positions=[synthetic_position("m0", 200000, 30000)]
        + [synthetic_position(m, 0, 5000 if m in ("m3", "m4") else 0) for m in ms[1:]],
        policies=_policies(ms),
        invoices=[InvoiceInput(invoice(o, a, b, f, due_days=4)) for o, a, b, f in pairs],
        labels=BASE_LABELS,
    )


def network_small_two_slot() -> Network:
    """A tiny network the brute-force reference can enumerate instantly."""
    from api.planning.generator import generate

    return generate("two_candidates", 7, max_obligations=3, slots=2)


# --- hardening fixtures (added after the canonical-p2-v1 benchmark; it is unchanged) ------
POSITIVE_SUM_ASSUMPTIONS = (
    "SYNTHETIC_ASSUMPTIONS (disclosed): food_supplier states early cash is worth 20%/yr to it "
    "(e.g. it would otherwise draw a costly credit line), declares a USD 2.00 cost of "
    "accepting changed terms, will not give more than USD 10.00, and requires a modeled net "
    "benefit >= USD 0.00; buyer hurdle 8%/yr and minimum USD 10.00 discount as in canonical"
)


def supplier_policy(**kw) -> MerchantPolicy:
    return MerchantPolicy("food_supplier", **kw)


def network_positive_sum() -> Network:
    """Both buyer and food supplier have a positive MODELED net benefit, under the
    disclosed synthetic assumptions above (not representative, not evidence)."""
    pol = supplier_policy(
        early_receipt_value_bps=2000,
        receipt_cost_cents=200,
        max_concession_given=usd(1000),
        min_receipt_net_benefit_cents=0,
    )
    net = network_two_candidates(supplier_policy=pol)
    return replace(
        net,
        network_id="synthetic_positive_sum",
        labels=(*net.labels, "CONSTRUCTED_EXAMPLE", POSITIVE_SUM_ASSUMPTIONS),
    )


def network_partially_paid() -> Network:
    """food_001 ($1,000) already had $400 applied before planning; a $10-off offer remains."""
    from api.domain.invoice import SettlementEvent, SettlementKind

    record = invoice("food_001", "buyer", "food_supplier", 100000).apply(
        SettlementEvent(
            "pay_prior_400",
            "food_001",
            SettlementKind.CASH_APPLIED,
            usd(40000),
            AS_OF + timedelta(minutes=1),
            SYN,
        )
    )
    record = early_discount(
        record,
        1000,
        status=RevisionStatus.DRAFT,
        authorized_by=frozenset({"food_supplier"}),
        minutes=5,
        expires_minutes=60,
    )
    c = clock()
    ms = ("buyer", "food_supplier", "carrier", "mechanic")
    return build_network(
        "synthetic_partially_paid",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[_buyer_position(PLAN_AS_OF, c)] + [synthetic_position(m) for m in ms[1:]],
        policies=_policies(ms, buyer=buyer_policy()),
        invoices=[InvoiceInput(record), *_downstream()],
        labels=BASE_LABELS,
    )


TIMING_ASSUMPTIONS = (
    "CONSTRUCTED_EXAMPLE (not representative): buyer has USD 1,000 now and a verified "
    "USD 1,000 receipt usable at slot 5; packaging invoice due day 8, food invoice due day 9, "
    "and the food supplier owes the carrier USD 900 due day 2. Buyer hurdle 0 and no minimum "
    "discount (it is indifferent to paying early)."
)


def network_timing_matters() -> Network:
    """Earliest-due-first pays packaging first and makes the carrier late; paying food
    first (both buyer invoices still on time) keeps everyone on time."""
    from api.planning.network import ScheduledInflow

    c = clock(slots=10)
    buyer = replace(
        synthetic_position("buyer", 100000, 0),
        inflows=(ScheduledInflow("rcpt_syn_buyer", usd(100000), 5, "synthetic"),),
    )
    ms = ("buyer", "food_supplier", "packaging_supplier", "carrier")
    return build_network(
        "constructed_timing_matters",
        as_of=PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[buyer] + [synthetic_position(m) for m in ms[1:]],
        policies=_policies(ms),
        invoices=[
            InvoiceInput(invoice("pack_001", "buyer", "packaging_supplier", 100000, due_days=8)),
            InvoiceInput(invoice("food_001", "buyer", "food_supplier", 100000, due_days=9)),
            InvoiceInput(invoice("freight_001", "food_supplier", "carrier", 90000, due_days=2)),
        ],
        labels=(*BASE_LABELS, "CONSTRUCTED_EXAMPLE", TIMING_ASSUMPTIONS),
    )
