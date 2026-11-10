"""The check script's send-by-id mode: never creates an invoice, sends only after checks,
and never prints the payer link. Uses a fake client; no PayPal call."""

import argparse
import datetime as dt
import importlib.util
from pathlib import Path

import pytest

from api.integrations.paypal.client import ProviderResponse

ROOT = Path(__file__).resolve().parents[2]
LINK = "https://www.sandbox.paypal.com/invoice/p/#INV2-TEST"
PAYER = "payer@example.com"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "paypal_sandbox_check", ROOT / "scripts" / "paypal_sandbox_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _usd(v):
    return {"currency_code": "USD", "value": v}


def _invoice(status="DRAFT"):
    return {
        "id": "INV2-TEST",
        "status": status,
        "detail": {
            "currency_code": "USD",
            "invoice_date": "2026-11-10",
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": "2026-12-01"},
        },
        "primary_recipients": [{"billing_info": {"email_address": PAYER}}],
        "items": [{"name": "x", "quantity": "1", "unit_amount": _usd("1000.00")}],
        "amount": {
            **_usd("990.00"),
            "breakdown": {
                "item_total": _usd("1000.00"),
                "discount": {"invoice_discount": {"amount": _usd("-10.00")}},
            },
        },
        "due_amount": _usd("990.00"),
    }


class FakeClient:
    def __init__(self, status="DRAFT"):
        self.status = status
        self.sent = 0

    def _resp(self, op, method, body, code=200):
        return ProviderResponse(op, method, "/x", code, body, "dbg")

    def get_invoice(self, invoice_id):
        return self._resp("get_invoice", "GET", _invoice(self.status))

    def send_invoice(self, invoice_id, request_id):
        self.sent += 1
        self.status = "UNPAID"
        return self._resp("send_invoice", "POST", {"rel": "payer-view", "href": LINK})

    def create_draft_invoice(self, *a, **k):
        raise AssertionError("send mode must never create an invoice")

    generate_invoice_number = create_draft_invoice
    update_invoice = create_draft_invoice


def _args():
    return argparse.Namespace(
        send_invoice_id="INV2-TEST",
        face_cents=100000,
        discount_cents=1000,
        expected_due_date=dt.date(2026, 12, 1),
    )


@pytest.fixture
def script(tmp_path, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def test_sends_once_after_checks_and_hides_link(script, tmp_path, capsys):
    client = FakeClient()
    rec = script.Recorder()
    assert script._send_existing(client, rec, _args(), PAYER) == 0
    assert client.sent == 1
    assert (tmp_path / "raw" / "payer_link_INV2-TEST.txt").read_text().strip() == LINK
    out = capsys.readouterr().out
    assert LINK not in out
    assert PAYER not in out
    assert LINK not in str(script.redact([r.get("response_redacted") for r in rec.records]))
    assert LINK not in str(rec.records)


def test_refuses_to_send_when_not_draft(script):
    client = FakeClient(status="SENT")
    rec = script.Recorder()
    assert script._send_existing(client, rec, _args(), PAYER) == 1
    assert client.sent == 0
    assert rec.records[-1]["verdict"] == "BLOCKED"


def test_refuses_to_send_to_other_recipient(script):
    client = FakeClient()
    rec = script.Recorder()
    assert script._send_existing(client, rec, _args(), "someone-else@example.com") == 1
    assert client.sent == 0
