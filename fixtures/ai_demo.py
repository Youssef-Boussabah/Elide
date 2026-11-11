"""SYNTHETIC P3 demo context (fixture version `ai-demo-v1`): merchant contexts for policy
drafting and the $5 -> $10 negotiation case, built from the canonical P1/P2 fixtures.

No real merchant data. The buyer's "current confirmed policy" (reserve $500, minimum
early discount $10) mirrors the canonical synthetic evidence and buyer policy.
"""

from __future__ import annotations

from api.domain.money import USD, Money
from api.negotiation.counteroffer import NegotiationCase
from api.planning.builder import InvoiceInput
from api.planning.network import SYNTHETIC_ZERO_LAG
from api.policy.validation import CurrentPolicy, MerchantContext
from fixtures import planning as fx
from fixtures.canonical import TZ, invoice, scenario_b_supplier_offer

FIXTURE_VERSION = "ai-demo-v1"
AS_OF = fx.PLAN_AS_OF
DEMO_MERCHANTS = ("buyer", "food_supplier", "packaging_supplier", "carrier", "mechanic")

_ALIASES = {
    "buyer": ("buyer", "the buyer", "caterer", "the caterer", "buyer (synthetic)"),
    "food_supplier": (
        "food_supplier",
        "food supplier",
        "the food supplier",
        "food supplier (synthetic)",
    ),
    "packaging_supplier": (
        "packaging_supplier",
        "packaging supplier",
        "the packaging supplier",
        "packaging supplier (synthetic)",
    ),
    "carrier": ("carrier", "the carrier", "carrier (synthetic)"),
    "mechanic": ("mechanic", "the mechanic", "mechanic (synthetic)"),
}
# Each merchant's own (permissioned) counterparties in the canonical network
_NEIGHBOURS = {
    "buyer": ("food_supplier", "packaging_supplier"),
    "food_supplier": ("buyer", "carrier"),
    "packaging_supplier": ("buyer",),
    "carrier": ("food_supplier", "mechanic"),
    "mechanic": ("carrier",),
}


def merchant_context(merchant_id: str) -> MerchantContext:
    if merchant_id not in DEMO_MERCHANTS:
        raise KeyError(merchant_id)
    aliases = {a: n for n in _NEIGHBOURS[merchant_id] for a in _ALIASES[n]}
    current = (
        CurrentPolicy(reserve_floor_cents=50000, min_early_discount_cents=1000)
        if merchant_id == "buyer"
        else CurrentPolicy()
    )
    return MerchantContext(
        merchant_id=merchant_id,
        business_tz=TZ,
        counterparties=aliases,
        current=current,
        evidence_reserve_cents=50000 if merchant_id == "buyer" else None,
    )


def negotiation_case(
    requester_max_cents: int = 1500, disclosed_max_cents: int | None = None, supplier_policy=None
) -> NegotiationCase:
    """Supplier offers $5 (signed); buyer needs >= $10 and can deploy $990."""
    c = fx.clock()
    ms = ("buyer", "food_supplier", "carrier", "mechanic")
    policies = fx._policies(
        ms,
        buyer=fx.buyer_policy(),
        **({"food_supplier": supplier_policy} if supplier_policy is not None else {}),
    )
    return NegotiationCase(
        network_id="ai_demo_negotiation",
        as_of=AS_OF,
        records=(
            InvoiceInput(scenario_b_supplier_offer()),
            InvoiceInput(invoice("freight_001", "food_supplier", "carrier", 90000)),
            InvoiceInput(invoice("repair_001", "carrier", "mechanic", 80000)),
        ),
        build={
            "clock": c,
            "availability": SYNTHETIC_ZERO_LAG,
            "focal_merchant": "buyer",
            "positions": [fx._buyer_position(AS_OF, c)]
            + [fx.synthetic_position(m) for m in ms[1:]],
            "policies": policies,
            "labels": (*fx.BASE_LABELS, f"FIXTURE:{FIXTURE_VERSION}"),
        },
        invoice_id="food_001",
        requester="buyer",
        counterparty="food_supplier",
        requester_max_concession=Money(requester_max_cents, USD),
        disclosed_max_concession=(
            None if disclosed_max_cents is None else Money(disclosed_max_cents, USD)
        ),
    )
