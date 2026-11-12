"""Independent verification of one reconciled capture (ADR-0012 §7.3, §17).

**Independence.** This module is coded separately from everything that builds the books:
it imports none of `settlement_postings`, `payer_postings`, `api.integrations.paypal.
settlement`, `invoice_terms` or the PayPal money boundary. It parses PayPal's raw GET
bodies with its own strict parser, reads **raw rows** with plain SQL, derives from the
provider's figures what every journal account must show, and compares. It reports any
disagreement and repairs nothing.

`verify_settlement` checks, for one capture:

1. Provider facts (own parser): the invoice is PAID with exactly one SUCCESS transaction,
   that transaction is this capture, the capture is COMPLETED and final and belongs to the
   invoice, item total - discount = total = paid, nothing due, no tax/shipping/other
   charges, gross - fee = net in one currency, and the credited amount is the net either
   unchanged (same currency, no rate) or converted at PayPal's own rate (to within half a
   minor unit).
2. Approved terms (raw rows): the obligation face, the session's expected item, discount
   and total, and the intent amount all equal the provider facts; the provider invoice is
   the session's; the payee is the bound invoicer account.
3. The settlement record: every amount, currency, rate, status, time and party equals the
   provider facts or the intent.
4. Every journal entry that cites the invoice or capture, or touches the invoice
   receivable or the obligation payable, is one of the expected entries, in the expected
   entity, with the expected evidence class, and its **debit and credit totals per account
   and currency** equal those derived from the provider facts (payee: issuance, concession,
   gross to clearing, fee, net, FX position, credited balance; payer: payable, discharge,
   concession, unverified payment source, optional merchant attestation). Balanced but
   wrong books (a moved fee, an inflated credit offset in the FX position, a misstated
   issuance) disagree here even though every entry balances.
5. Settlement events: exactly one cash-applied event for this intent equal to gross; the
   obligation's identity face = cash applied + concession + remaining with remaining 0.
6. Cash commitments: the reservation closed as settled by this capture; one settled
   outflow of the gross in the payer's books (a conservative hold; see ADR-0012 §17.2).

The result is a canonical **verification document** (checker version, input body hashes,
the extracted provider facts, a digest of every row examined, the problems) and its
SHA-256. The same rows and bodies always produce the same document and digest.

`check_capture` is the earlier, ledger-identity-only check (kept for scripts and the
historical evidence); `verify_settlement` supersedes it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, text

from api.integrations.paypal.redaction import fingerprint

CHECKER_VERSION = "elide-reconciliation-check/2"
PLACEHOLDER_SHA = "0" * 64

# The chart-of-accounts contract the checker verifies (ADR-0012 §17.1).
AR = "accounts_receivable:{invoice}"
AP = "accounts_payable:{obligation}"
REVENUE = "revenue:sales"
CONTRA = "contra_revenue:early_payment_discount"
CLEARING = "paypal_settlement_clearing"
FEES = "expense:paypal_fees"
FX = "fx_conversion_position"
BALANCE = "paypal_balance"
PURCHASES = "purchases"
DISCOUNT_RECEIVED = "discount_received"
SENT = "paypal_payments_sent"
UNVERIFIED = "payment_source_unverified:{capture}"
THIRD_PARTY = "third_party_payment:{capture}"

SANDBOX = "sandbox_verified"
ATTESTED = "merchant_attested"


# -- canonical form ------------------------------------------------------------------------
def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def body_sha256(body: Any) -> str:
    """The hash `provider_operation.response_sha256` records for a provider body."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _norm(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


def _row_sha(row: dict[str, Any]) -> str:
    return sha256_hex(canonical(_norm(row)))


def document_digest(document: dict[str, Any]) -> str:
    return sha256_hex(canonical(document))


# -- provider facts (own strict parser) ----------------------------------------------------
_AMOUNT = re.compile(r"^-?\d{1,12}(\.\d{1,2})?$")
_CUR = re.compile(r"^[A-Z]{3}$")


class FactsError(ValueError):
    pass


def _amount(obj: Any, what: str) -> tuple[int, str]:
    if not isinstance(obj, dict):
        raise FactsError(f"{what}: not a money object")
    cur, val = obj.get("currency_code"), obj.get("value")
    if not isinstance(cur, str) or not _CUR.match(cur):
        raise FactsError(f"{what}: bad currency {cur!r}")
    if not isinstance(val, str) or not _AMOUNT.match(val):
        raise FactsError(f"{what}: bad value {val!r}")
    cents = Decimal(val) * 100
    if cents != cents.to_integral_value():
        raise FactsError(f"{what}: more than two decimals")
    return int(cents), cur


def _time(v: Any, what: str) -> str:
    if not isinstance(v, str):
        raise FactsError(f"{what}: missing")
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError as exc:
        raise FactsError(f"{what}: unreadable") from exc


@dataclass(frozen=True, slots=True)
class ProviderFacts:
    invoice_id: str
    invoice_status: str
    invoice_currency: str
    item_total_cents: int
    discount_cents: int
    total_cents: int
    paid_cents: int
    due_cents: int
    transactions: tuple[tuple[str, str], ...]  # (payment_id, transaction_status)
    other_charges: tuple[str, ...]
    capture_id: str
    capture_status: str
    final_capture: bool
    capture_invoice_id: str | None
    capture_amount_cents: int
    gross_cents: int
    fee_cents: int
    net_cents: int
    gross_currency: str
    fee_currency: str
    net_currency: str
    credit_cents: int
    credit_currency: str
    rate: str | None
    rate_source: str | None
    rate_target: str | None
    capture_create_time: str
    payee_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return _norm(dataclasses.asdict(self))


def extract_facts(inv: dict[str, Any], cap: dict[str, Any]) -> ProviderFacts:
    if not isinstance(inv, dict) or not isinstance(cap, dict):
        raise FactsError("provider bodies must be objects")
    amount = inv.get("amount") or {}
    total, inv_cur = _amount(amount, "invoice.amount")
    bd = amount.get("breakdown") or {}
    item, item_cur = _amount(bd.get("item_total"), "invoice.breakdown.item_total")
    disc_obj = ((bd.get("discount") or {}).get("invoice_discount") or {}).get("amount")
    discount, disc_cur = (0, inv_cur) if disc_obj is None else _amount(disc_obj, "discount")
    discount = abs(discount)
    other = []
    for k in ("tax_total", "shipping", "custom"):
        if bd.get(k) is not None:
            v = bd[k].get("amount") if isinstance(bd[k], dict) and "amount" in bd[k] else bd[k]
            if _amount(v, f"invoice.breakdown.{k}")[0] != 0:
                other.append(k)
    if ((bd.get("discount") or {}).get("item_discount") or {}).get("value") not in (
        None,
        "0",
        "0.00",
    ):
        other.append("item_discount")
    pays = inv.get("payments") or {}
    paid, paid_cur = _amount(pays.get("paid_amount"), "invoice.payments.paid_amount")
    due, due_cur = _amount(inv.get("due_amount"), "invoice.due_amount")
    if len({inv_cur, item_cur, disc_cur, paid_cur, due_cur}) != 1:
        raise FactsError("invoice amounts are in more than one currency")
    txns = tuple(
        (str(t.get("payment_id")), str(t.get("transaction_status")))
        for t in (pays.get("transactions") or [])
    )
    srb = cap.get("seller_receivable_breakdown") or {}
    gross, gcur = _amount(srb.get("gross_amount"), "capture.gross_amount")
    fee, fcur = _amount(srb.get("paypal_fee"), "capture.paypal_fee")
    net, ncur = _amount(srb.get("net_amount"), "capture.net_amount")
    cap_amt, _ = _amount(cap.get("amount"), "capture.amount")
    if srb.get("receivable_amount") is not None:
        credit, ccur = _amount(srb.get("receivable_amount"), "capture.receivable_amount")
    else:
        credit, ccur = net, ncur
    er = srb.get("exchange_rate")
    rate = src = tgt = None
    if er is not None:
        if not isinstance(er, dict):
            raise FactsError("capture.exchange_rate: not an object")
        rate, src, tgt = er.get("value"), er.get("source_currency"), er.get("target_currency")
        try:
            if Decimal(str(rate)) <= 0:
                raise FactsError("capture.exchange_rate: not positive")
        except InvalidOperation as exc:
            raise FactsError("capture.exchange_rate: unreadable") from exc
    payee = (cap.get("payee") or {}).get("merchant_id")
    if not payee:
        raise FactsError("capture.payee.merchant_id: missing")
    for k, v in (("invoice.id", inv.get("id")), ("capture.id", cap.get("id"))):
        if not isinstance(v, str) or not v:
            raise FactsError(f"{k}: missing")
    return ProviderFacts(
        invoice_id=inv["id"],
        invoice_status=str(inv.get("status")),
        invoice_currency=inv_cur,
        item_total_cents=item,
        discount_cents=discount,
        total_cents=total,
        paid_cents=paid,
        due_cents=due,
        transactions=txns,
        other_charges=tuple(other),
        capture_id=cap["id"],
        capture_status=str(cap.get("status")),
        final_capture=cap.get("final_capture") is True,
        capture_invoice_id=cap.get("invoice_id"),
        capture_amount_cents=cap_amt,
        gross_cents=gross,
        fee_cents=fee,
        net_cents=net,
        gross_currency=gcur,
        fee_currency=fcur,
        net_currency=ncur,
        credit_cents=credit,
        credit_currency=ccur,
        rate=None if rate is None else str(rate),
        rate_source=src,
        rate_target=tgt,
        capture_create_time=_time(cap.get("create_time"), "capture.create_time"),
        payee_fingerprint=fingerprint(payee),
    )


def facts_problems(f: ProviderFacts) -> list[str]:
    p: list[str] = []
    if f.invoice_status != "PAID":
        p.append(f"provider: invoice status {f.invoice_status}, expected PAID")
    if f.transactions != ((f.capture_id, "SUCCESS"),):
        p.append(f"provider: invoice transactions {list(f.transactions)}, expected this capture")
    if f.other_charges:
        p.append(f"provider: unexpected invoice charges {list(f.other_charges)}")
    if f.item_total_cents - f.discount_cents != f.total_cents:
        p.append("provider: item total - discount != invoice total")
    if f.paid_cents != f.total_cents or f.due_cents != 0:
        p.append(f"provider: paid {f.paid_cents} due {f.due_cents} of total {f.total_cents}")
    if f.capture_status != "COMPLETED" or not f.final_capture:
        p.append(f"provider: capture {f.capture_status} final={f.final_capture}")
    if f.capture_invoice_id != f.invoice_id:
        p.append("provider: the capture belongs to another invoice")
    if len({f.gross_currency, f.fee_currency, f.net_currency, f.invoice_currency}) != 1:
        p.append("provider: gross, fee, net and invoice currencies differ")
    if f.capture_amount_cents != f.gross_cents or f.gross_cents != f.paid_cents:
        p.append("provider: capture amount, gross and invoice paid amount differ")
    if f.fee_cents < 0 or f.gross_cents - f.fee_cents != f.net_cents:
        p.append(f"provider: gross {f.gross_cents} - fee {f.fee_cents} != net {f.net_cents}")
    if f.credit_currency == f.net_currency:
        if f.rate is not None or f.credit_cents != f.net_cents:
            p.append("provider: a same-currency credit must equal net with no rate")
    elif (f.rate_source, f.rate_target) != (f.net_currency, f.credit_currency):
        p.append("provider: exchange rate direction does not match the credit")
    else:
        exact = Decimal(f.net_cents) * Decimal(f.rate or "0")
        if abs(exact - f.credit_cents) > Decimal("0.5"):
            p.append(
                f"provider: credit {f.credit_cents} {f.credit_currency} is not net "
                f"{f.net_cents} converted at {f.rate} ({exact})"
            )
    return p


# -- raw rows --------------------------------------------------------------------------------
def _one(conn: Connection, sql: str, **params: Any) -> dict[str, Any] | None:
    r = conn.execute(text(sql), params).mappings().first()
    return None if r is None else dict(r)


def _all(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _movements(lines: list[dict[str, Any]]) -> dict[tuple[str, str], tuple[int, int]]:
    dr: dict[tuple[str, str], int] = defaultdict(int)
    cr: dict[tuple[str, str], int] = defaultdict(int)
    for ln in lines:
        k = (ln["account"], ln["currency"])
        n = int(ln["amount_cents"])
        if n >= 0:
            dr[k] += n
        else:
            cr[k] += -n
    return {k: (dr.get(k, 0), cr.get(k, 0)) for k in set(dr) | set(cr)}


def _expect(*moves: tuple[str, str, int, int]) -> dict[tuple[str, str], tuple[int, int]]:
    out: dict[tuple[str, str], list[int]] = {}
    for acc, cur, d, c in moves:
        if d == 0 and c == 0:
            continue
        a = out.setdefault((acc, cur), [0, 0])
        a[0] += d
        a[1] += c
    return {k: (v[0], v[1]) for k, v in out.items()}


def _compare(label: str, actual: dict, expected: dict, problems: list[str]) -> None:
    for k in sorted(set(actual) | set(expected)):
        a, e = actual.get(k, (0, 0)), expected.get(k, (0, 0))
        if a != e:
            problems.append(
                f"{label}: {k[0]} {k[1]} debits {a[0]} credits {a[1]}, "
                f"expected debits {e[0]} credits {e[1]}"
            )


@dataclass(frozen=True, slots=True)
class Verification:
    checker_version: str
    capture_id: str
    document: dict[str, Any]
    digest: str
    problems: list[str]

    @property
    def passed(self) -> bool:
        return not self.problems


def verify_settlement(
    conn: Connection,
    *,
    capture_id: str,
    invoice_body: dict[str, Any],
    capture_body: dict[str, Any],
    require_recorded_reads: bool = True,
) -> Verification:
    """Verify one capture's books against PayPal's own figures (module docstring).

    `require_recorded_reads`: the bodies must be ones Elide's provider operations recorded
    for this intent (true at booking; a later re-verification with fresh GETs reports
    whether they still match, as information)."""
    problems: list[str] = []
    records: dict[str, Any] = {}
    inputs: dict[str, Any] = {
        "invoice_body_sha256": body_sha256(invoice_body),
        "capture_body_sha256": body_sha256(capture_body),
    }
    try:
        f: ProviderFacts | None = extract_facts(invoice_body, capture_body)
    except FactsError as exc:
        f = None
        problems.append(f"provider: unreadable body ({exc})")
    inputs["provider_facts"] = None if f is None else f.as_dict()
    if f is not None:
        problems += facts_problems(f)
        if f.capture_id != capture_id:
            problems.append("provider: the capture body is for another capture")

    st = _one(conn, "SELECT * FROM provider_settlement WHERE capture_id = :c", c=capture_id)
    if st is None:
        problems.append("no settlement record for this capture")
        return _finish(capture_id, inputs, records, problems)
    records["provider_settlement"] = {"capture_id": capture_id, "row_sha256": _row_sha(st)}
    intent = _one(
        conn,
        "SELECT intent_id, authorization_id, obligation_id, payer_merchant, payee_merchant, "
        "amount_cents, currency, commitment_id FROM payment_intent WHERE intent_id = :i",
        i=st["intent_id"],
    )
    sess = _one(
        conn,
        "SELECT session_id, intent_id, provider_invoice_id, invoice_number, invoicer_account_id, "
        "payer_account_id, expected_item_cents, expected_discount_cents, expected_total_cents, "
        "currency FROM payment_session WHERE intent_id = :i",
        i=st["intent_id"],
    )
    if intent is None or sess is None:
        problems.append("the settlement's intent or payment session is missing")
        return _finish(capture_id, inputs, records, problems)
    obl = _one(
        conn,
        "SELECT obligation_id, debtor, creditor, currency, face_cents FROM obligation "
        "WHERE obligation_id = :o",
        o=intent["obligation_id"],
    )
    binding = _one(
        conn,
        "SELECT account_id, merchant_id, role, account_fingerprint, revoked_at "
        "FROM provider_account WHERE account_id = :a",
        a=sess["invoicer_account_id"],
    )
    for name, row in (("payment_intent", intent), ("payment_session", sess), ("obligation", obl)):
        if row is not None:
            records[name] = _row_sha(row)
    if binding is not None:
        records["invoicer_binding"] = _row_sha(
            {k: binding[k] for k in binding if k != "revoked_at"}
        )
    if obl is None or binding is None:
        problems.append("the obligation or the invoicer binding is missing")
        return _finish(capture_id, inputs, records, problems)

    payer, payee = intent["payer_merchant"], intent["payee_merchant"]
    inv_id = st["invoice_id"]
    face, cash, concession = (
        int(obl["face_cents"]),
        int(sess["expected_total_cents"]),
        int(sess["expected_discount_cents"]),
    )

    # 2. approved terms vs provider facts
    if (obl["debtor"], obl["creditor"]) != (payer, payee) or obl["currency"] != "USD":
        problems.append("terms: obligation parties or currency differ from the intent")
    if int(sess["expected_item_cents"]) != face:
        problems.append("terms: session item total differs from the obligation face")
    if int(intent["amount_cents"]) != cash or intent["currency"] != "USD":
        problems.append("terms: intent amount differs from the session total")
    if sess["provider_invoice_id"] != inv_id:
        problems.append("terms: the settlement's invoice is not the session's invoice")
    if binding["role"] != "invoicer" or binding["merchant_id"] != payee:
        problems.append("terms: the invoicer binding is not the payee's")
    if f is not None:
        if f.invoice_id != inv_id:
            problems.append("provider: invoice body is for another invoice")
        if f.invoice_currency != "USD":
            problems.append("provider: invoice currency is not USD")
        for what, got, want in (
            ("item total vs obligation face", f.item_total_cents, face),
            ("discount vs approved concession", f.discount_cents, concession),
            ("total vs approved amount", f.total_cents, cash),
            ("gross vs approved amount", f.gross_cents, cash),
        ):
            if got != want:
                problems.append(f"provider vs terms: {what}: {got} != {want}")
        if f.payee_fingerprint != binding["account_fingerprint"]:
            problems.append("provider: capture payee is not the bound invoicer account")

    # 3. the settlement record vs provider facts
    if f is not None:
        want_rec = {
            "provider": "paypal",
            "environment": "sandbox",
            "invoice_id": f.invoice_id,
            "intent_id": intent["intent_id"],
            "payer_merchant": payer,
            "payee_merchant": payee,
            "gross_cents": f.gross_cents,
            "gross_currency": f.gross_currency,
            "fee_cents": f.fee_cents,
            "net_cents": f.net_cents,
            "credit_cents": f.credit_cents,
            "credit_currency": f.credit_currency,
            "capture_status": f.capture_status,
            "invoice_status": f.invoice_status,
            "capture_create_time": f.capture_create_time,
            "payee_fingerprint": f.payee_fingerprint,
        }
        got_rec = _norm({k: st[k] for k in want_rec})
        for k in want_rec:
            if got_rec[k] != want_rec[k]:
                problems.append(
                    f"settlement record: {k} {got_rec[k]!r} != provider {want_rec[k]!r}"
                )
        if (st["exchange_rate"] is None) != (f.rate is None) or (
            f.rate is not None and Decimal(st["exchange_rate"]) != Decimal(f.rate)
        ):
            problems.append(f"settlement record: exchange_rate {st['exchange_rate']} != {f.rate}")
    if st["check_sha256"] not in (None, PLACEHOLDER_SHA):
        problems.append("settlement record: check_sha256 must be NULL (legacy rows: placeholder)")

    # 4. journal
    ar, ap = AR.format(invoice=inv_id), AP.format(obligation=intent["obligation_id"])
    entries = _all(
        conn,
        "SELECT id, entity_id, kind, evidence_class, idempotency_key, content_sha256, "
        "provider_resource_type, provider_resource_id FROM journal_entry e "
        "WHERE (provider = 'paypal' AND provider_resource_id IN (:inv, :cap)) "
        "OR EXISTS (SELECT 1 FROM journal_posting p WHERE p.entry_id = e.id AND "
        "((e.entity_id = :payee AND p.account = :ar) "
        "OR (e.entity_id = :payer AND p.account = :ap)))"
        " ORDER BY id",
        inv=inv_id,
        cap=capture_id,
        payee=payee,
        payer=payer,
        ar=ar,
        ap=ap,
    )
    lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if entries:
        for ln in _all(
            conn,
            "SELECT entry_id, line_no, account, currency, amount_cents FROM journal_posting "
            "WHERE entry_id = ANY(:ids) ORDER BY entry_id, line_no",
            ids=[e["id"] for e in entries],
        ):
            lines[int(ln["entry_id"])].append(ln)
    records["journal_entries"] = [
        {
            "id": int(e["id"]),
            "kind": e["kind"],
            "entity_id": e["entity_id"],
            "content_sha256": e["content_sha256"],
            "postings_sha256": sha256_hex(canonical(_norm(lines[int(e["id"])]))),
        }
        for e in entries
    ]
    attest = _one(
        conn,
        "SELECT id, capture_id, intent_id, merchant_id, source, journal_entry_id "
        "FROM payment_source_attestation WHERE capture_id = :c",
        c=capture_id,
    )
    corrections = _all(
        conn,
        "SELECT subject_table, subject_id, aspect, corrected_to FROM evidence_correction "
        "WHERE subject_table = 'journal_entry' AND subject_id = ANY(:ids)",
        ids=[str(e["id"]) for e in entries] or [""],
    )
    if attest is not None:
        records["payment_source_attestation"] = _row_sha(attest)
    records["evidence_corrections"] = sorted(_row_sha(c) for c in corrections)
    corrected = {(c["subject_id"], c["aspect"]) for c in corrections}

    if f is not None:
        _check_journal(
            entries, lines, f, face, concession, payee, payer, ar, ap, attest, corrected, problems
        )

    # 5. settlement events
    ev = _all(
        conn,
        "SELECT event_id, kind, amount_cents, currency, occurred_at, intent_id, "
        "reverses_event_id FROM obligation_settlement_event WHERE obligation_id = :o "
        "ORDER BY event_id",
        o=intent["obligation_id"],
    )
    records["settlement_events"] = [
        {"event_id": e["event_id"], "row_sha256": _row_sha(e)} for e in ev
    ]
    mine = [e for e in ev if e["intent_id"] == intent["intent_id"] and e["kind"] == "cash_applied"]
    if f is not None and (
        len(mine) != 1
        or int(mine[0]["amount_cents"]) != f.gross_cents
        or mine[0]["currency"] != "USD"
    ):
        problems.append(f"settlement events: expected one cash_applied of {f.gross_cents} USD")
    applied = sum(int(e["amount_cents"]) for e in ev if e["kind"] == "cash_applied") - sum(
        int(e["amount_cents"]) for e in ev if e["kind"] == "cash_reversed"
    )
    if applied != cash:
        problems.append(f"settlement events: cash applied {applied}, expected {cash}")
    if face - applied - concession != 0:
        problems.append(
            f"settlement identity: face {face} = cash {applied} + concession {concession} "
            f"+ remaining {face - applied - concession}; remaining must be 0"
        )

    # 6. commitments
    cm = _all(
        conn,
        "SELECT commitment_id, merchant_id, currency, amount_cents, kind, created_at, "
        "released_at, release_reason FROM capacity_commitment "
        "WHERE commitment_id IN (:r, :s) ORDER BY commitment_id",
        r=intent["commitment_id"],
        s=f"so:{intent['intent_id']}",
    )
    records["commitments"] = [
        {"commitment_id": c["commitment_id"], "row_sha256": _row_sha(c)} for c in cm
    ]
    by_id = {c["commitment_id"]: c for c in cm}
    res = by_id.get(intent["commitment_id"])
    if (
        res is None
        or res["kind"] != "reservation"
        or res["release_reason"] != (f"settled:capture:{capture_id}")
    ):
        problems.append("commitments: the reservation was not closed as settled by this capture")
    so = by_id.get(f"so:{intent['intent_id']}")
    if (
        so is None
        or so["kind"] != "settled_outflow"
        or so["merchant_id"] != payer
        or so["currency"] != "USD"
        or int(so["amount_cents"]) != cash
        or (
            so["released_at"] is not None
            and not str(so["release_reason"]).startswith("reflected_in_evidence:")
        )
    ):
        problems.append(f"commitments: expected a settled outflow of {cash} USD for the payer")

    # Provider reads: are these the bodies Elide's own GETs recorded for this intent?
    reads = _all(
        conn,
        "SELECT operation, response_sha256 FROM provider_operation WHERE intent_id = :i "
        "AND operation IN ('get_invoice', 'get_capture') AND state = 'succeeded'",
        i=intent["intent_id"],
    )
    recorded = {(r["operation"], r["response_sha256"]) for r in reads}
    matches = {
        "invoice": ("get_invoice", inputs["invoice_body_sha256"]) in recorded,
        "capture": ("get_capture", inputs["capture_body_sha256"]) in recorded,
    }
    inputs["bodies_match_recorded_reads"] = matches
    if require_recorded_reads and not all(matches.values()):
        problems.append(f"inputs: bodies are not ones recorded by provider reads: {matches}")
    return _finish(capture_id, inputs, records, problems)


def _check_journal(
    entries, lines, f, face, concession, payee, payer, ar, ap, attest, corrected, problems
):
    cap = f.capture_id
    unverified, third = UNVERIFIED.format(capture=cap), THIRD_PARTY.format(capture=cap)
    fx = f.credit_currency != f.gross_currency
    usd = f.gross_currency
    payee_settlement = [
        (CLEARING, usd, f.gross_cents, f.fee_cents + f.net_cents),
        (ar, usd, 0, f.gross_cents),
        (FEES, usd, f.fee_cents, 0),
    ]
    if fx:
        payee_settlement += [
            (FX, usd, f.net_cents, 0),
            (BALANCE, f.credit_currency, f.credit_cents, 0),
            (FX, f.credit_currency, 0, f.credit_cents),
        ]
    else:
        payee_settlement += [(BALANCE, usd, f.net_cents, 0)]
    # (kind, entity, resource type, evidence class) -> expected movements
    spec: dict[str, tuple[str, str | None, str, dict]] = {
        "invoice_issued": (
            payee,
            "invoice",
            SANDBOX,
            _expect((ar, usd, face, 0), (REVENUE, usd, 0, face)),
        ),
        "provider_settlement": (payee, "capture", SANDBOX, _expect(*payee_settlement)),
        "payable_recognized": (
            payer,
            None,
            ATTESTED,
            _expect((PURCHASES, usd, face, 0), (ap, usd, 0, face)),
        ),
    }
    if concession:
        spec["concession_authorized"] = (
            payee,
            "invoice",
            SANDBOX,
            _expect((CONTRA, usd, concession, 0), (ar, usd, 0, concession)),
        )
    kinds = defaultdict(list)
    for e in entries:
        kinds[e["kind"]].append(e)
    legacy = kinds.get("provider_payment_sent", [])
    current = kinds.get("obligation_discharged", [])
    if len(legacy) + len(current) != 1:
        problems.append(
            f"journal: expected one payer discharge entry, found {len(legacy) + len(current)}"
        )
    elif current:
        spec["obligation_discharged"] = (
            payer,
            "capture",
            SANDBOX,
            _expect(
                (ap, usd, face, 0),
                (unverified, usd, 0, f.gross_cents),
                (DISCOUNT_RECEIVED, usd, 0, concession),
            ),
        )
    else:
        spec["provider_payment_sent"] = (
            payer,
            "capture",
            SANDBOX,
            _expect(
                (ap, usd, face, 0),
                (SENT, usd, 0, f.gross_cents),
                (DISCOUNT_RECEIVED, usd, 0, concession),
            ),
        )
        if (str(legacy[0]["id"]), "payer_source_debit") not in corrected:
            problems.append(
                "journal: legacy payer entry claims a verified outflow without an "
                "evidence correction"
            )
    att_entries = kinds.get("payment_source_attested", [])
    if attest is None:
        if att_entries:
            problems.append("journal: payment-source entry without an attestation")
    else:
        src, credit_acc = (
            attest["source"],
            SENT if attest["source"] == "own_paypal_account" else third,
        )
        debit_acc = unverified if current else (SENT if src == "third_party" else None)
        if attest["merchant_id"] != payer:
            problems.append("attestation: not made by the payer")
        if debit_acc is None:
            if att_entries or attest["journal_entry_id"] is not None:
                problems.append("journal: legacy own-account attestation must not post")
        else:
            spec["payment_source_attested"] = (
                payer,
                "capture",
                ATTESTED,
                _expect((debit_acc, usd, f.gross_cents, 0), (credit_acc, usd, 0, f.gross_cents)),
            )
            if len(att_entries) == 1 and attest["journal_entry_id"] != att_entries[0]["id"]:
                problems.append("attestation: linked to another journal entry")
    seen = set()
    for e in entries:
        k = e["kind"]
        if k not in spec:
            problems.append(f"journal: unexpected {k} entry {e['id']} touches this payment")
            continue
        if k in seen:
            problems.append(f"journal: more than one {k} entry")
            continue
        seen.add(k)
        entity, rtype, evc, want = spec[k]
        if e["entity_id"] != entity:
            problems.append(f"journal: {k} is in {e['entity_id']}'s books, expected {entity}'s")
        if rtype is not None and (e["provider_resource_type"], e["provider_resource_id"]) != (
            rtype,
            f.invoice_id if rtype == "invoice" else cap,
        ):
            problems.append(f"journal: {k} does not cite the provider {rtype}")
        if e["evidence_class"] != evc:
            problems.append(f"journal: {k} evidence class {e['evidence_class']}, expected {evc}")
        _compare(f"journal {k}", _movements(lines[int(e["id"])]), want, problems)
    for k in sorted(set(spec) - seen):
        problems.append(f"journal: missing {k} entry")


def _finish(capture_id, inputs, records, problems) -> Verification:
    document = {
        "checker_version": CHECKER_VERSION,
        "capture_id": capture_id,
        "inputs": inputs,
        "records": records,
        "problems": problems,
    }
    document = json.loads(canonical(_norm(document)))
    return Verification(CHECKER_VERSION, capture_id, document, document_digest(document), problems)


# -- the earlier ledger-identity check -------------------------------------------------------
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
    return _all(
        conn,
        "SELECT id, entity_id, kind FROM journal_entry WHERE provider = 'paypal' "
        "AND provider_environment = 'sandbox' AND provider_resource_type = :t "
        "AND provider_resource_id = :r",
        t=resource_type,
        r=resource_id,
    )


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
    """Ledger identities only (checker v1): balance, cleared receivable and payable, cash to
    clearing, the payer-side discharge, the settlement identity. It cannot see balanced but
    wrong books; `verify_settlement` can."""
    problems: list[str] = []
    cap = _entries(conn, "capture", x.capture_id)
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for e in cap:
        by_kind[e["kind"]].append(e)
    if len(by_kind["provider_settlement"]) != 1:
        problems.append(f"expected 1 settlement entry, found {len(by_kind['provider_settlement'])}")
    discharge = by_kind["provider_payment_sent"] + by_kind["obligation_discharged"]
    if len(discharge) != 1:
        problems.append(f"expected 1 payer discharge entry, found {len(discharge)}")
    for e in cap + _entries(conn, "invoice", x.invoice_id):
        totals: dict[str, int] = defaultdict(int)
        for _, cur, n in _postings(conn, e["id"]):
            totals[cur] += n
        if any(v != 0 for v in totals.values()):
            problems.append(f"entry {e['id']} does not balance: {dict(totals)}")

    ar = _balance(conn, x.payee_entity, AR.format(invoice=x.invoice_id))
    if any(v != 0 for v in ar.values()):
        problems.append(f"payee receivable not cleared: {ar}")
    for e in by_kind["provider_settlement"]:
        if e["entity_id"] != x.payee_entity:
            problems.append("settlement entry is not in the payee's books")
        clearing_dr = sum(
            n for a, c, n in _postings(conn, e["id"]) if a == CLEARING and c == "USD" and n > 0
        )
        if clearing_dr != x.cash_cents:
            problems.append(f"clearing received {clearing_dr}, expected {x.cash_cents}")

    rec = _one(
        conn,
        "SELECT gross_cents, fee_cents, net_cents, gross_currency, payee_merchant, "
        "payer_merchant FROM provider_settlement WHERE capture_id = :c",
        c=x.capture_id,
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

    ap = _balance(conn, x.payer_entity, AP.format(obligation=x.obligation_id))
    if any(v != 0 for v in ap.values()):
        problems.append(f"payer payable not cleared: {ap}")
    unverified = UNVERIFIED.format(capture=x.capture_id)
    for e in discharge:
        if e["entity_id"] != x.payer_entity:
            problems.append("payer discharge entry is not in the payer's books")
        out = -sum(n for a, c, n in _postings(conn, e["id"]) if a in (SENT, unverified))
        if out != x.cash_cents:
            problems.append(f"payer discharge {out}, expected {x.cash_cents}")

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
