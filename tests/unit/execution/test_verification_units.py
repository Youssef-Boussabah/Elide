"""Units of the independent checker's own provider parser (ADR-0012 §17.1). No database.

Bodies are synthetic, shaped like the PayPal GET responses the pilot recorded (the real
pilot figures USD 24.00 / 1.00 / 23.00 -> CAD 31.57 at 1.37241975 are used as numbers only).
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from api.accounting import reconciliation_check as rc

ROOT = Path(__file__).resolve().parents[3]


def _m(v, cur="USD"):
    return {"currency_code": cur, "value": v}


INV = {
    "id": "INV2-TEST",
    "status": "PAID",
    "amount": {
        **_m("24.00"),
        "breakdown": {
            "item_total": _m("25.00"),
            "discount": {"invoice_discount": {"amount": _m("-1.00")}},
        },
    },
    "due_amount": _m("0.00"),
    "payments": {
        "paid_amount": _m("24.00"),
        "transactions": [{"payment_id": "CAP1", "transaction_status": "SUCCESS"}],
    },
}
CAP = {
    "id": "CAP1",
    "status": "COMPLETED",
    "final_capture": True,
    "invoice_id": "INV2-TEST",
    "amount": _m("24.00"),
    "seller_receivable_breakdown": {
        "gross_amount": _m("24.00"),
        "paypal_fee": _m("1.00"),
        "net_amount": _m("23.00"),
        "receivable_amount": _m("31.57", "CAD"),
        "exchange_rate": {
            "source_currency": "USD",
            "target_currency": "CAD",
            "value": "1.37241975",
        },
    },
    "payee": {"merchant_id": "PAYEE1"},
    "create_time": "2026-11-12T18:24:35Z",
}


def _facts(inv=INV, cap=CAP):
    return rc.extract_facts(copy.deepcopy(inv), copy.deepcopy(cap))


def test_pilot_shaped_bodies_have_no_problems():
    f = _facts()
    assert (f.item_total_cents, f.discount_cents, f.total_cents) == (2500, 100, 2400)
    assert (f.gross_cents, f.fee_cents, f.net_cents, f.credit_cents) == (2400, 100, 2300, 3157)
    assert rc.facts_problems(f) == []


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        (("seller_receivable_breakdown", "receivable_amount"), _m("31.59", "CAD"), "converted"),
        (("seller_receivable_breakdown", "paypal_fee"), _m("1.10"), "gross"),
        (("seller_receivable_breakdown", "net_amount"), _m("22.90"), "gross"),
        (("status",), "PENDING", "capture PENDING"),
        (("final_capture",), False, "final=False"),
        (("invoice_id",), "INV2-OTHER", "another invoice"),
        (("amount",), _m("25.00"), "capture amount"),
        (
            ("seller_receivable_breakdown", "exchange_rate"),
            {"source_currency": "CAD", "target_currency": "USD", "value": "1.37241975"},
            "direction",
        ),
    ],
)
def test_capture_tampering_is_reported(path, value, needle):
    cap = copy.deepcopy(CAP)
    node = cap
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    problems = rc.facts_problems(_facts(cap=cap))
    assert any(needle in s for s in problems), problems


def test_invoice_tampering_is_reported():
    inv = copy.deepcopy(INV)
    inv["status"] = "PARTIALLY_PAID"
    inv["due_amount"] = _m("1.00")
    inv["payments"]["transactions"].append({"payment_id": "CAP2", "transaction_status": "SUCCESS"})
    inv["amount"]["breakdown"]["shipping"] = {"amount": _m("2.00")}
    p = rc.facts_problems(_facts(inv=inv))
    for needle in ("status PARTIALLY_PAID", "transactions", "charges", "due 100"):
        assert any(needle in s for s in p), (needle, p)


def test_same_currency_credit_must_equal_net_without_a_rate():
    cap = copy.deepcopy(CAP)
    srb = cap["seller_receivable_breakdown"]
    del srb["exchange_rate"]
    srb["receivable_amount"] = _m("23.00")
    assert rc.facts_problems(_facts(cap=cap)) == []
    srb["receivable_amount"] = _m("23.01")
    assert rc.facts_problems(_facts(cap=cap))


@pytest.mark.parametrize("bad", ["24", "24.001", "1e3", "", None, 24, "24.00 "])
def test_amounts_are_parsed_strictly(bad):
    cap = copy.deepcopy(CAP)
    cap["seller_receivable_breakdown"]["gross_amount"] = {"currency_code": "USD", "value": bad}
    if bad == "24":  # an integer string is an exact amount
        assert rc.facts_problems(_facts(cap=cap)) == []
        return
    with pytest.raises(rc.FactsError):
        _facts(cap=cap)


def test_canonical_digest_is_order_independent_and_versioned():
    a = {"b": 1, "a": [1, {"y": 2, "x": 1}]}
    b = {"a": [1, {"x": 1, "y": 2}], "b": 1}
    assert rc.document_digest(a) == rc.document_digest(b)
    assert rc.CHECKER_VERSION.endswith("/2")
    # The body hash is the one provider_operation.response_sha256 records.
    from api.execution.service import _sha

    assert rc.body_sha256(CAP) == _sha(CAP)


def test_the_checker_does_not_import_the_booking_implementation():
    """Independence (ADR-0012 §17.1): the checker shares no code with what it checks."""
    src = (ROOT / "api" / "accounting" / "reconciliation_check.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    forbidden = {
        "api.accounting.settlement_postings",
        "api.accounting.payer_postings",
        "api.integrations.paypal.settlement",
        "api.integrations.paypal.invoice_terms",
        "api.integrations.paypal.money",
        "api.execution.service",
        "api.domain.settlement",
        "api.domain.money",
    }
    assert not imported & forbidden, imported & forbidden
