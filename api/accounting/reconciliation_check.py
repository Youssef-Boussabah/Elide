"""Independent consistency check of one reconciled capture (ADR-0012 §7.3).

Coded separately from the posting builders (`settlement_postings`, `payer_postings`) and
the execution service: it reads **raw rows** (journal entries, postings, settlement events,
the settlement record) with plain SQL and re-derives every identity from them. It must
agree with the booking; any disagreement is reported, never repaired.

Checks:
- exactly one `provider_settlement` entry and one `provider_payment_sent` entry cite the
  capture; every entry of the bundle balances per currency;
- payee: the invoice receivable `accounts_receivable:<invoice>` nets to zero in every
  currency; the clearing account received exactly the expected cash (gross);
- gross - fee = net (USD), and the settlement record agrees with the postings;
- payer: `accounts_payable:<obligation>` nets to zero; `paypal_payments_sent` was credited
  exactly the expected cash;
- obligation: cash applied (applied - reversed) from settlement events equals the expected
  cash, and face = cash applied + authorized concession + remaining open with remaining 0.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import Connection, text


@dataclass(frozen=True, slots=True)
class CaptureExpectation:
    capture_id: str
    invoice_id: str
    payee_entity: str
    payer_entity: str
    obligation_id: str
    face_cents: int
    cash_cents: int
    concession_cents: int


def _entries(conn: Connection, resource_type: str, resource_id: str) -> list[dict]:
    rows = conn.execute(
        text(
            "SELECT id, entity_id, kind FROM journal_entry WHERE provider = 'paypal' "
            "AND provider_environment = 'sandbox' AND provider_resource_type = :t "
            "AND provider_resource_id = :r"
        ),
        {"t": resource_type, "r": resource_id},
    ).mappings()
    return [dict(r) for r in rows]


def _postings(conn: Connection, entry_id: int) -> list[tuple[str, str, int]]:
    return [
        (a, c, int(n))
        for a, c, n in conn.execute(
            text(
                "SELECT account, currency, amount_cents FROM journal_posting "
                "WHERE entry_id = :e ORDER BY line_no"
            ),
            {"e": entry_id},
        ).all()
    ]


def _balance(conn: Connection, entity: str, account: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT p.currency, SUM(p.amount_cents) FROM journal_posting p "
            "JOIN journal_entry e ON e.id = p.entry_id "
            "WHERE e.entity_id = :ent AND p.account = :acc GROUP BY p.currency"
        ),
        {"ent": entity, "acc": account},
    ).all()
    return {c: int(s) for c, s in rows}


def check_capture(conn: Connection, x: CaptureExpectation) -> list[str]:
    problems: list[str] = []
    cap = _entries(conn, "capture", x.capture_id)
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for e in cap:
        by_kind[e["kind"]].append(e)
    if len(by_kind["provider_settlement"]) != 1:
        problems.append(f"expected 1 settlement entry, found {len(by_kind['provider_settlement'])}")
    if len(by_kind["provider_payment_sent"]) != 1:
        problems.append(
            f"expected 1 payment-sent entry, found {len(by_kind['provider_payment_sent'])}"
        )
    for e in cap + _entries(conn, "invoice", x.invoice_id):
        totals: dict[str, int] = defaultdict(int)
        for _, cur, n in _postings(conn, e["id"]):
            totals[cur] += n
        if any(v != 0 for v in totals.values()):
            problems.append(f"entry {e['id']} does not balance: {dict(totals)}")

    ar = _balance(conn, x.payee_entity, f"accounts_receivable:{x.invoice_id}")
    if any(v != 0 for v in ar.values()):
        problems.append(f"payee receivable not cleared: {ar}")
    for e in by_kind["provider_settlement"]:
        if e["entity_id"] != x.payee_entity:
            problems.append("settlement entry is not in the payee's books")
        lines = _postings(conn, e["id"])
        clearing_dr = sum(
            n for a, c, n in lines if a == "paypal_settlement_clearing" and c == "USD" and n > 0
        )
        if clearing_dr != x.cash_cents:
            problems.append(f"clearing received {clearing_dr}, expected {x.cash_cents}")

    rec = (
        conn.execute(
            text(
                "SELECT gross_cents, fee_cents, net_cents, gross_currency, payee_merchant, "
                "payer_merchant FROM provider_settlement WHERE capture_id = :c"
            ),
            {"c": x.capture_id},
        )
        .mappings()
        .one_or_none()
    )
    if rec is None:
        problems.append("no settlement record")
    else:
        if int(rec["gross_cents"]) != x.cash_cents or rec["gross_currency"] != "USD":
            problems.append("settlement record gross differs from the expected cash")
        if int(rec["gross_cents"]) - int(rec["fee_cents"]) != int(rec["net_cents"]):
            problems.append("settlement record: gross - fee != net")
        if (rec["payee_merchant"], rec["payer_merchant"]) != (x.payee_entity, x.payer_entity):
            problems.append("settlement record parties differ")

    ap = _balance(conn, x.payer_entity, f"accounts_payable:{x.obligation_id}")
    if any(v != 0 for v in ap.values()):
        problems.append(f"payer payable not cleared: {ap}")
    for e in by_kind["provider_payment_sent"]:
        if e["entity_id"] != x.payer_entity:
            problems.append("payment-sent entry is not in the payer's books")
        sent = -sum(n for a, c, n in _postings(conn, e["id"]) if a == "paypal_payments_sent")
        if sent != x.cash_cents:
            problems.append(f"payer outflow {sent}, expected {x.cash_cents}")

    ev = conn.execute(
        text(
            "SELECT kind, amount_cents FROM obligation_settlement_event "
            "WHERE obligation_id = :o AND currency = 'USD'"
        ),
        {"o": x.obligation_id},
    ).all()
    applied = sum(int(n) for k, n in ev if k == "cash_applied") - sum(
        int(n) for k, n in ev if k == "cash_reversed"
    )
    if applied != x.cash_cents:
        problems.append(f"cash applied to the obligation {applied}, expected {x.cash_cents}")
    remaining = x.face_cents - applied - x.concession_cents
    if remaining != 0:
        problems.append(
            f"settlement identity: face {x.face_cents} = cash {applied} + concession "
            f"{x.concession_cents} + remaining {remaining}; remaining must be 0"
        )
    return problems
