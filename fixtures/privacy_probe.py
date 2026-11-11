"""SYNTHETIC privacy-probe network (`privacy-probe-v1`) with distinctive private values.

Every private figure and identifier is unusual on purpose so a leak is easy to detect:
buyer spendable USD 1,777.77 and reserve USD 512.34; a hidden buyer counterparty
`secret_vendor` with invoice `secret_inv_9` (USD 4,321.09); food supplier reserve
USD 123.45; carrier -> mechanic invoice `repair_77` hidden from the buyer.
"""

from __future__ import annotations

from api.planning.builder import InvoiceInput, build_network, position_from_evidence
from api.planning.network import SYNTHETIC_ZERO_LAG, Network
from fixtures import planning as fx
from fixtures.canonical import invoice, synthetic_evidence

PRIVATE = {
    "buyer": (
        "1,777.77",
        "1777.77",
        "177777",
        "512.34",
        "51234",
        "secret_vendor",
        "secret_inv_9",
        "4,321.09",
        "4321.09",
        "432109",
    ),
    "food_supplier": ("123.45", "12345"),
    "carrier": ("repair_77",),
    "mechanic": ("repair_77",),
}


def network_privacy_probe() -> Network:
    c = fx.clock()
    ev = synthetic_evidence("ev_buyer_probe", "buyer", 177777, 51234)
    ms = ("buyer", "food_supplier", "secret_vendor", "carrier", "mechanic")
    return build_network(
        "synthetic_privacy_probe",
        as_of=fx.PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[
            position_from_evidence(ev, as_of=fx.PLAN_AS_OF, clock=c),
            fx.synthetic_position("food_supplier", 0, 12345),
            fx.synthetic_position("secret_vendor"),
            fx.synthetic_position("carrier"),
            fx.synthetic_position("mechanic"),
        ],
        policies=fx._policies(ms, buyer=fx.buyer_policy()),
        invoices=[
            InvoiceInput(fx._offer("food_001", "food_supplier", 1000)),
            InvoiceInput(invoice("secret_inv_9", "buyer", "secret_vendor", 432109)),
            InvoiceInput(invoice("freight_001", "food_supplier", "carrier", 85000)),
            InvoiceInput(invoice("repair_77", "carrier", "mechanic", 80000)),
        ],
        labels=(*fx.BASE_LABELS, "FIXTURE:privacy-probe-v1"),
    )
