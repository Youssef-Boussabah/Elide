"""P5 final architectural review: independent financial verification, payer attribution
and the settlement verification attestation (ADR-0012 §17). **Synthetic-tested**: mock
PayPal and synthetic fixtures only; the real Sandbox pilot is recorded separately in
`evidence/phase5/`.

The tamper suite simulates a defective journal or settlement builder: each tamper is
balanced (the database's per-currency balance guard accepts it) and keeps the receivable,
clearing account and payable at zero, so ledger identities alone cannot see it. Only an
independent comparison of the raw rows with the provider's own figures can. Every tampered
booking must be refused atomically: nothing posted, nothing settled, an operator exception.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from api.accounting.journal import EntryKind, Posting
from api.domain.money import Money, SignedAmount
from api.execution.tables import payment_exception, provider_settlement

pytestmark = pytest.mark.integration

PILOT_RATE = "1.37241975"


def _shift(entry, dr: str, cr: str, currency: str, cents: int):
    """A balanced extra pair inside one entry: Dr `dr` / Cr `cr` by `cents`."""
    m = Money(cents, currency)
    return dataclasses.replace(
        entry,
        postings=(
            *entry.postings,
            Posting(dr, SignedAmount.debit(m)),
            Posting(cr, SignedAmount.credit(m)),
        ),
    )


def _with_kind(entries, kind, fn):
    return tuple(fn(e) if e.kind is kind else e for e in entries)


def _sent(p, *, fx: bool):
    if fx:
        p.mock.settlement_currency = "CAD"
        p.mock.fx_rate = Decimal(PILOT_RATE)
    p.bind()
    _, intent = p.agree_and_approve()
    iid = intent["intent_id"]
    p.work()
    assert p.intent(iid)["status"] == "payer_action_pending"
    return iid


def _pay(p, iid):
    cap = p.mock.pay(p.invoice_id(iid))
    p.work()
    return cap


def _refused(p, iid, cap):
    with p.engine.connect() as c:
        posted = c.execute(
            text("SELECT count(*) FROM journal_entry WHERE provider_resource_id IN (:c, :i)"),
            {"c": cap, "i": p.invoice_id(iid)},
        ).scalar()
        settled = c.execute(select(func.count()).select_from(provider_settlement)).scalar()
        codes = c.execute(
            select(payment_exception.c.code, payment_exception.c.detail).where(
                payment_exception.c.intent_id == iid
            )
        ).all()
    return p.intent(iid)["status"], posted, settled, codes


def _assert_refused(p, iid, cap, needle: str):
    status, posted, settled, codes = _refused(p, iid, cap)
    assert status == "exception", (status, codes)
    assert posted == 0 and settled == 0
    assert [c for c, _ in codes] == ["INDEPENDENT_CHECK_FAILED"]
    problems = codes[0][1]["problems"]
    assert any(needle in s for s in problems), problems


# -- tamper suite: balanced but wrong books --------------------------------------------------
def test_wrong_cad_credit_offset_in_fx_position_is_refused(pilot, monkeypatch):
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=True)
    real = svc.settlement_entries
    monkeypatch.setattr(
        svc,
        "settlement_entries",
        lambda s, e: _with_kind(
            real(s, e),
            EntryKind.PROVIDER_SETTLEMENT,
            lambda x: _shift(x, "paypal_balance", "fx_conversion_position", "CAD", 100),
        ),
    )
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "paypal_balance")


def test_fee_misallocation_that_keeps_balance_is_refused(pilot, monkeypatch):
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=True)
    real = svc.settlement_entries
    monkeypatch.setattr(
        svc,
        "settlement_entries",
        lambda s, e: _with_kind(
            real(s, e),
            EntryKind.PROVIDER_SETTLEMENT,
            lambda x: _shift(x, "expense:paypal_fees", "fx_conversion_position", "USD", 50),
        ),
    )
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "expense:paypal_fees")


def test_same_currency_fee_misallocation_is_refused(pilot, monkeypatch):
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=False)
    real = svc.settlement_entries
    monkeypatch.setattr(
        svc,
        "settlement_entries",
        lambda s, e: _with_kind(
            real(s, e),
            EntryKind.PROVIDER_SETTLEMENT,
            lambda x: _shift(x, "paypal_balance", "expense:paypal_fees", "USD", 33),
        ),
    )
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "expense:paypal_fees")


def test_internally_consistent_but_wrong_fx_conversion_is_refused(pilot, monkeypatch):
    """The parsed settlement uses another rate and credit; record and journal agree with
    each other (the domain checks pass) but not with PayPal's capture."""
    import api.execution.service as svc
    from api.domain.money import ExchangeRate

    p = pilot
    iid = _sent(p, fx=True)
    real = svc.settlement_from_bodies

    def wrong(*a, **kw):
        s = real(*a, **kw)
        rate = ExchangeRate("USD", "CAD", Decimal("1.40"))
        return dataclasses.replace(s, exchange_rate=rate, settlement_credit=rate.convert(s.net))

    monkeypatch.setattr(svc, "settlement_from_bodies", wrong)
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "credit")


def test_internally_consistent_but_wrong_net_is_refused(pilot, monkeypatch):
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=True)
    real = svc.settlement_from_bodies

    def wrong(*a, **kw):
        s = real(*a, **kw)
        fee = s.provider_fee + Money(10, "USD")
        net = s.gross - fee
        return dataclasses.replace(
            s, provider_fee=fee, net=net, settlement_credit=s.exchange_rate.convert(net)
        )

    monkeypatch.setattr(svc, "settlement_from_bodies", wrong)
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "fee")


def test_wrong_issuance_without_concession_still_clearing_is_refused(pilot, monkeypatch):
    """Invoice booked at the paid amount and the concession dropped: the receivable still
    clears (24 - 24 = 0) but revenue and the concession are misstated."""
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=False)
    real = svc.settlement_entries

    def wrong(s, e):
        out = []
        for x in real(s, e):
            if x.kind is EntryKind.CONCESSION_AUTHORIZED:
                continue
            if x.kind is EntryKind.INVOICE_ISSUED:
                m = s.gross
                x = dataclasses.replace(
                    x,
                    postings=(
                        Posting(f"accounts_receivable:{s.invoice_id}", SignedAmount.debit(m)),
                        Posting("revenue:sales", SignedAmount.credit(m)),
                    ),
                )
            out.append(x)
        return tuple(out)

    monkeypatch.setattr(svc, "settlement_entries", wrong)
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "invoice_issued")


def test_concession_posted_to_the_wrong_account_is_refused(pilot, monkeypatch):
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=False)
    real = svc.settlement_entries
    monkeypatch.setattr(
        svc,
        "settlement_entries",
        lambda s, e: _with_kind(
            real(s, e),
            EntryKind.CONCESSION_AUTHORIZED,
            lambda x: _shift(
                x, "revenue:sales", "contra_revenue:early_payment_discount", "USD", 100
            ),
        ),
    )
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "concession_authorized")


def test_payer_payable_overstated_with_matching_discount_is_refused(pilot, monkeypatch):
    """Payer: purchases and the payable inflated by 1.00, offset as extra discount; the
    payable clears and the outflow is unchanged."""
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=False)
    real = svc.payer_entries

    def wrong(**kw):
        out = []
        for x in real(**kw):
            ap = f"accounts_payable:{kw['obligation_id']}"
            if x.kind is EntryKind.PAYABLE_RECOGNIZED:
                x = _shift(x, "purchases", ap, "USD", 100)
            else:
                x = _shift(x, ap, "discount_received", "USD", 100)
            out.append(x)
        return tuple(out)

    monkeypatch.setattr(svc, "payer_entries", wrong)
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "purchases")


# -- payer attribution and the verification attestation -------------------------------------
def test_payer_cash_outflow_is_not_labelled_provider_verified(pilot):
    """PayPal's capture does not identify the paying account: no payer-books posting that
    asserts the payer's own cash left may be classified as Sandbox-verified."""
    p = pilot
    iid = _sent(p, fx=False)
    cap = _pay(p, iid)
    assert p.intent(iid)["status"] == "reconciled"
    with p.engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT e.kind, e.evidence_class, p.account FROM journal_entry e "
                "JOIN journal_posting p ON p.entry_id = e.id "
                "WHERE e.entity_id = 'pilot_buyer' AND p.account = 'paypal_payments_sent'"
            )
        ).all()
    assert all(ev != "sandbox_verified" for _, ev, _ in rows), rows
    assert cap


def _bodies(p, iid, cap):
    return p.mock.invoices[p.invoice_id(iid)], p.mock.captures[cap]


def _verify(p, iid, cap, **kw):
    from api.accounting.reconciliation_check import verify_settlement

    inv, cb = _bodies(p, iid, cap)
    with p.engine.connect() as c:
        return verify_settlement(c, capture_id=cap, invoice_body=inv, capture_body=cb, **kw)


def _reconciled(p, *, fx: bool = True):
    iid = _sent(p, fx=fx)
    cap = _pay(p, iid)
    assert p.intent(iid)["status"] == "reconciled"
    return iid, cap


def _balance(p, entity, account):
    with p.engine.connect() as c:
        return {
            cur: int(n)
            for cur, n in c.execute(
                text(
                    "SELECT p.currency, SUM(p.amount_cents) FROM journal_posting p JOIN "
                    "journal_entry e ON e.id = p.entry_id WHERE e.entity_id = :e "
                    "AND p.account = :a GROUP BY p.currency"
                ),
                {"e": entity, "a": account},
            ).all()
        }


def _insert_verification(c, purpose, verdict, problems, digest):
    c.execute(
        text(
            "INSERT INTO settlement_verification (capture_id, intent_id, checker_version, "
            "purpose, verdict, problems, document, document_sha256, invoice_body_sha256, "
            "capture_body_sha256, verified_at, actor) SELECT capture_id, intent_id, 'x', "
            ":purpose, :verdict, CAST(:problems AS jsonb), '{}', :digest, repeat('a', 64), "
            "repeat('a', 64), now(), 'test' FROM provider_settlement"
        ),
        {"purpose": purpose, "verdict": verdict, "problems": problems, "digest": digest},
    )


# -- the verification attestation (finding 3) -----------------------------------------------
def test_booking_writes_a_reproducible_verification_attestation(pilot):
    from api.accounting.reconciliation_check import CHECKER_VERSION, canonical, sha256_hex
    from api.execution.tables import settlement_verification

    p = pilot
    iid, cap = _reconciled(p)
    with p.engine.connect() as c:
        st = c.execute(select(provider_settlement)).mappings().one()
        v = c.execute(select(settlement_verification)).mappings().one()
        last = c.execute(
            text(
                "SELECT detail FROM payment_state_event WHERE intent_id = :i "
                "AND to_status = 'reconciled'"
            ),
            {"i": iid},
        ).scalar_one()
    assert st["check_sha256"] is None  # never a placeholder again
    assert (v["purpose"], v["verdict"], v["problems"]) == ("booking", "passed", [])
    assert v["checker_version"] == CHECKER_VERSION
    # The digest is the SHA-256 of the stored canonical document ...
    assert sha256_hex(canonical(v["document"])) == v["document_sha256"]
    assert last["verification_sha256"] == v["document_sha256"]
    # ... it names the provider bodies and the exact records examined ...
    doc = v["document"]
    assert doc["inputs"]["bodies_match_recorded_reads"] == {"invoice": True, "capture": True}
    assert doc["inputs"]["provider_facts"]["credit_currency"] == "CAD"
    kinds = sorted(e["kind"] for e in doc["records"]["journal_entries"])
    assert kinds == [
        "concession_authorized",
        "invoice_issued",
        "obligation_discharged",
        "payable_recognized",
        "provider_settlement",
    ]
    # ... and re-running the checker on the same rows and bodies reproduces it exactly.
    again = _verify(p, iid, cap)
    assert again.problems == [] and again.digest == v["document_sha256"]
    assert again.document == doc


def test_a_different_provider_body_changes_the_digest_and_fails(pilot):
    p = pilot
    iid, cap = _reconciled(p)
    inv, cb = _bodies(p, iid, cap)
    from api.accounting.reconciliation_check import verify_settlement

    cb = {**cb, "seller_receivable_breakdown": {**cb["seller_receivable_breakdown"]}}
    cb["seller_receivable_breakdown"]["paypal_fee"] = {"currency_code": "USD", "value": "0.01"}
    with p.engine.connect() as c:
        v = verify_settlement(c, capture_id=cap, invoice_body=inv, capture_body=cb)
    assert v.problems and v.digest != _verify(p, iid, cap).digest
    assert any("not ones recorded by provider reads" in s for s in v.problems)


def test_verification_and_settlement_constraints_hold_in_the_database(pilot):
    from sqlalchemy.exc import DBAPIError

    p = pilot
    _reconciled(p)
    with p.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO evidence_correction (subject_table, subject_id, aspect, "
                "recorded_as, corrected_to, reason, corrected_by, corrected_at) VALUES "
                "('provider_settlement', 'X', 'test', 'a', 'b', 'c', 'test', now())"
            )
        )
    for sql in (
        "UPDATE settlement_verification SET verdict = 'failed'",
        "DELETE FROM settlement_verification",
        "UPDATE evidence_correction SET reason = 'x'",
        "DELETE FROM evidence_correction",
        "UPDATE provider_settlement SET check_sha256 = NULL",
    ):
        with pytest.raises(DBAPIError), p.engine.begin() as c:
            c.execute(text(sql))
    # A second booking verification for the same capture is refused.
    with pytest.raises(DBAPIError), p.engine.begin() as c:
        _insert_verification(c, "booking", "passed", "[]", "a" * 64)
    # A passed verdict with problems, or a malformed digest, is refused.
    with pytest.raises(DBAPIError), p.engine.begin() as c:
        _insert_verification(c, "reverification", "passed", '["p"]', "a" * 64)
    with pytest.raises(DBAPIError), p.engine.begin() as c:
        _insert_verification(c, "reverification", "failed", '["p"]', "not-a-digest")


def test_a_settlement_that_skips_the_checker_cannot_commit(pilot, monkeypatch):
    """The deferred constraint trigger: a booking path that writes the settlement but no
    passing verification is rolled back at commit, with every other row of the booking."""
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=False)
    from types import SimpleNamespace

    # A defective booking that "forgets" the verification: its insert becomes a no-op.
    skip = SimpleNamespace(insert=lambda: SimpleNamespace(values=lambda **kw: text("SELECT 1")))
    monkeypatch.setattr(svc, "settlement_verification", skip)
    cap = _pay(p, iid)
    status, posted, settled, codes = _refused(p, iid, cap)
    assert status == "exception" and posted == 0 and settled == 0
    assert [c for c, _ in codes] == ["DUPLICATE_OR_CONFLICTING_POSTING"]
    assert "no passing booking verification" in str(codes[0][1])


# -- payer attribution (finding 2) -----------------------------------------------------------
def test_discharge_is_provider_confirmed_but_the_payment_source_is_not(pilot):
    p = pilot
    iid, cap = _reconciled(p)
    # The payable is discharged (the debt is paid whoever paid it) ...
    assert _balance(p, "pilot_buyer", "accounts_payable:pilot_001") == {"USD": 0}
    # ... but no posting asserts the payer's own cash left: it sits in the unverified source.
    assert _balance(p, "pilot_buyer", f"payment_source_unverified:{cap}") == {"USD": -2400}
    assert _balance(p, "pilot_buyer", "paypal_payments_sent") == {}
    buyer, seller = p.actor(p.buyer_login), p.actor(p.seller_login)
    mine = buyer.get("/v1/m/pilot_buyer/settlements").json()["settlements"][0]
    assert mine["role"] == "payer" and "paid_cents" not in mine
    assert mine["settled_cents"] == 2400
    assert mine["obligation_settlement"] == "PROVIDER_CONFIRMED"
    assert mine["payer_authorization"] == "MERCHANT_APPROVED"
    assert mine["payment_source"] == "NOT_VERIFIED"
    assert mine["outflow_commitment_basis"] == "PRESUMED_OUTFLOW_HELD_CONSERVATIVELY"
    assert mine["verification"]["status"] == "PASSED"
    assert set(mine["verification"]["attestations"][0]) == {
        "purpose",
        "verdict",
        "checker_version",
        "document_sha256",
        "verified_at",
    }
    detail = buyer.get(f"/v1/m/pilot_buyer/payment-intents/{iid}").json()["settlement"]
    assert detail["payment_source"] == "NOT_VERIFIED"
    theirs = seller.get("/v1/m/pilot_seller/settlements").json()["settlements"][0]
    assert theirs["paying_account"] == "NOT_IDENTIFIED_BY_PROVIDER"
    assert "payment_source" not in theirs  # the payer's own statements stay private


def _attest(p, iid, source, key):
    buyer = p.actor(p.buyer_login)
    buyer.reauth()
    return buyer.post(
        f"/v1/m/pilot_buyer/payment-intents/{iid}/payment-source", {"source": source}, key=key
    )


def _outflow_released(p, iid):
    with p.engine.connect() as c:
        return c.execute(
            text("SELECT released_at FROM capacity_commitment WHERE commitment_id = :s"),
            {"s": f"so:{iid}"},
        ).scalar_one()


def test_payer_attests_own_account_once_and_books_stay_verified(pilot):
    p = pilot
    iid, cap = _reconciled(p)
    r = _attest(p, iid, "own_paypal_account", "source-own-1")
    assert r.status_code == 200, r.text
    assert r.json()["payment_source"] == "MERCHANT_ATTESTED_OWN_ACCOUNT"
    assert r.json()["cash_commitment_changed"] is False
    assert _balance(p, "pilot_buyer", f"payment_source_unverified:{cap}") == {"USD": 0}
    assert _balance(p, "pilot_buyer", "paypal_payments_sent") == {"USD": -2400}
    with p.engine.connect() as c:
        ev = c.execute(
            text("SELECT evidence_class FROM journal_entry WHERE kind = 'payment_source_attested'")
        ).scalar_one()
    assert ev == "merchant_attested"
    # Same key: the stored response. Another key: refused, nothing posts twice.
    assert _attest(p, iid, "own_paypal_account", "source-own-1").json()["replayed"] is True
    again = _attest(p, iid, "third_party", "source-own-2")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "ALREADY_ATTESTED"
    # The payee cannot attest the payer's source.
    seller = p.actor(p.seller_login)
    seller.reauth()
    r = seller.post(
        f"/v1/m/pilot_seller/payment-intents/{iid}/payment-source",
        {"source": "own_paypal_account"},
        key="source-seller-1",
    )
    assert r.status_code == 404
    assert _outflow_released(p, iid) is None
    # The independent checker still agrees, attestation included.
    assert _verify(p, iid, cap).problems == []


def test_third_party_payment_discharges_debt_without_inventing_a_payer_outflow(pilot):
    p = pilot
    iid, cap = _reconciled(p)
    r = _attest(p, iid, "third_party", "source-third-1")
    assert r.status_code == 200 and r.json()["payment_source"] == "MERCHANT_ATTESTED_THIRD_PARTY"
    assert _balance(p, "pilot_buyer", "accounts_payable:pilot_001") == {"USD": 0}
    assert _balance(p, "pilot_buyer", "paypal_payments_sent") == {}
    assert _balance(p, "pilot_buyer", f"third_party_payment:{cap}") == {"USD": -2400}
    # The settled outflow stays a conservative hold; only balance evidence releases it.
    assert _outflow_released(p, iid) is None
    assert _verify(p, iid, cap).problems == []


def test_an_attestation_entry_that_disagrees_is_caught(pilot):
    """A tampered attestation posting (wrong amount, balanced) fails re-verification."""
    from api.accounting.journal import EvidenceClass, JournalEntry
    from api.accounting.store import post_entry

    p = pilot
    iid, cap = _reconciled(p)
    assert _attest(p, iid, "own_paypal_account", "source-own-x").status_code == 200
    m = Money(100, "USD")
    post_entry(
        p.engine,
        JournalEntry(
            entity_id="pilot_buyer",
            idempotency_key="test:tamper:source",
            kind=EntryKind.PAYABLE_RECOGNIZED,
            evidence_class=EvidenceClass.MERCHANT_ATTESTED,
            description="tamper: a second payable against the same obligation",
            occurred_at=p.now,
            postings=(
                Posting("purchases", SignedAmount.debit(m)),
                Posting("accounts_payable:pilot_001", SignedAmount.credit(m)),
            ),
        ),
    )
    problems = _verify(p, iid, cap).problems
    assert any("more than one payable_recognized" in s for s in problems), problems


def test_concurrent_attestations_record_exactly_one(pilot):
    import threading

    from api.accounting.payer_postings import PaymentSource
    from api.execution.attribution import attest_payment_source
    from api.workflow.store import ConflictError

    p = pilot
    iid, _cap = _reconciled(p)
    with p.engine.connect() as c:
        uid = c.execute(text("SELECT user_id FROM payment_authorization LIMIT 1")).scalar_one()
    barrier, out = threading.Barrier(4), []

    def go(n):
        barrier.wait()
        src = PaymentSource.THIRD_PARTY if n % 2 else PaymentSource.OWN_PAYPAL_ACCOUNT
        try:
            with p.engine.begin() as c:
                attest_payment_source(
                    c, merchant_id="pilot_buyer", user_id=uid, intent_id=iid, source=src, at=p.now
                )
            out.append("ok")
        except ConflictError as exc:
            out.append(exc.code)

    ts = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(out) == ["ALREADY_ATTESTED"] * 3 + ["ok"]
    with p.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM payment_source_attestation")).scalar() == 1
        n = c.execute(
            text("SELECT count(*) FROM journal_entry WHERE kind = 'payment_source_attested'")
        ).scalar()
    assert n == 1


# -- a pre-0008 (legacy) booking -------------------------------------------------------------
def _legacy_payer_entries(**kw):
    """The pre-0008 builder in effect: one provider_payment_sent entry crediting
    paypal_payments_sent as sandbox_verified."""
    from api.accounting.journal import EvidenceClass, JournalEntry, ProviderEventRef

    ap = f"accounts_payable:{kw['obligation_id']}"
    owed = kw["cash"] + kw["concession"]
    sent = [
        Posting(ap, SignedAmount.debit(owed)),
        Posting("paypal_payments_sent", SignedAmount.credit(kw["cash"])),
    ]
    if kw["concession"].cents:
        sent.append(Posting("discount_received", SignedAmount.credit(kw["concession"])))
    return (
        JournalEntry(
            entity_id=kw["payer_entity"],
            idempotency_key=f"elide:obligation:{kw['obligation_id'].lower()}:payable_recognized",
            kind=EntryKind.PAYABLE_RECOGNIZED,
            evidence_class=EvidenceClass.MERCHANT_ATTESTED,
            description="legacy payable",
            occurred_at=kw["recognized_at"],
            postings=(
                Posting("purchases", SignedAmount.debit(owed)),
                Posting(ap, SignedAmount.credit(owed)),
            ),
        ),
        JournalEntry(
            entity_id=kw["payer_entity"],
            idempotency_key=f"paypal:sandbox:capture:{kw['capture_id'].lower()}:payment_sent",
            kind=EntryKind.PROVIDER_PAYMENT_SENT,
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            description="legacy payment sent",
            occurred_at=kw["paid_at"],
            postings=tuple(sent),
            provider_ref=ProviderEventRef("paypal", "sandbox", "capture", kw["capture_id"]),
        ),
    )


def test_a_legacy_shaped_payer_entry_is_refused_without_its_correction(pilot, monkeypatch):
    """The checker will not accept the pre-0008 'verified outflow' claim on a new booking:
    only the migration's append-only correction (for rows that already existed) relabels
    it as unverified. The full legacy path (correction, re-verification, attestations) is
    exercised by `test_legacy_booking_is_corrected_reverified_and_attestable`."""
    import api.execution.service as svc

    p = pilot
    iid = _sent(p, fx=True)
    monkeypatch.setattr(svc, "payer_entries", _legacy_payer_entries)
    cap = _pay(p, iid)
    _assert_refused(p, iid, cap, "legacy payer entry")


def _book_like_pre_0008(p, monkeypatch, iid):
    """Book one capture at schema 0007 exactly as the pre-0008 code did: the legacy payer
    entry, the 64-zero check_sha256, no verification row; then migrate to head."""
    from types import SimpleNamespace

    import api.execution.service as svc
    from api.accounting.reconciliation_check import Verification
    from tests.integration.conftest import migrate

    url = p.engine.url.render_as_string(hide_password=False)
    p.engine.dispose()
    migrate(url, "0007_p5_request_identity", "down")
    p.engine.dispose()
    real_ps = svc.provider_settlement
    with monkeypatch.context() as mp:
        mp.setattr(svc, "payer_entries", _legacy_payer_entries)
        mp.setattr(
            svc,
            "verify_settlement",
            lambda conn, **kw: Verification(
                "legacy",
                kw["capture_id"],
                {"inputs": {"invoice_body_sha256": "", "capture_body_sha256": ""}},
                "",
                [],
            ),
        )
        mp.setattr(
            svc,
            "settlement_verification",
            SimpleNamespace(insert=lambda: SimpleNamespace(values=lambda **kw: text("SELECT 1"))),
        )
        mp.setattr(
            svc,
            "provider_settlement",
            SimpleNamespace(
                c=real_ps.c,
                insert=lambda: SimpleNamespace(
                    values=lambda **kw: real_ps.insert().values(**{**kw, "check_sha256": "0" * 64})
                ),
            ),
        )
        cap = _pay(p, iid)
    assert p.intent(iid)["status"] == "reconciled"
    p.engine.dispose()
    migrate(url, "head")
    p.engine.dispose()
    return cap


@pytest.mark.parametrize("source", ["own_paypal_account", "third_party"])
def test_legacy_booking_is_corrected_reverified_and_attestable(pilot, monkeypatch, source):
    p = pilot
    iid = _sent(p, fx=True)
    cap = _book_like_pre_0008(p, monkeypatch, iid)
    with p.engine.connect() as c:
        st = c.execute(select(provider_settlement)).mappings().one()
        corr = {
            (t, a): to
            for t, a, to in c.execute(
                text("SELECT subject_table, aspect, corrected_to FROM evidence_correction")
            ).all()
        }
        legacy_ev = c.execute(
            text("SELECT evidence_class FROM journal_entry WHERE kind = 'provider_payment_sent'")
        ).scalar_one()
    # The immutable facts are unchanged: still the placeholder, still the recorded class ...
    assert st["check_sha256"] == "0" * 64 and legacy_ev == "sandbox_verified"
    # ... and the migration appended the corrected provenance beside them.
    assert corr == {
        ("journal_entry", "payer_source_debit"): "unverified",
        ("provider_settlement", "payer_merchant"): "debtor_of_discharged_obligation",
        ("provider_settlement", "check_sha256"): "placeholder_not_a_digest",
        ("capacity_commitment", "outflow_basis"): "presumed_outflow_unverified_source",
    }
    # The new checker verifies the legacy books against PayPal's bodies (with the correction).
    assert _verify(p, iid, cap).problems == []
    buyer = p.actor(p.buyer_login)
    mine = buyer.get("/v1/m/pilot_buyer/settlements").json()["settlements"][0]
    assert mine["payment_source"] == "NOT_VERIFIED"
    assert mine["verification"]["legacy_check_sha256_placeholder"] is True
    assert mine["verification"]["status"] == "NOT_ATTESTED"
    assert {"aspect": "payer_merchant", "corrected_to": "debtor_of_discharged_obligation"} in mine[
        "corrections"
    ]
    # Attestation on a legacy booking: own account posts nothing; third party compensates.
    r = _attest(p, iid, source, f"legacy-{source}")
    assert r.status_code == 200, r.text
    assert r.json()["journal_entry_posted"] is (source == "third_party")
    sent = _balance(p, "pilot_buyer", "paypal_payments_sent")
    third = _balance(p, "pilot_buyer", f"third_party_payment:{cap}")
    if source == "third_party":
        assert sent == {"USD": 0} and third == {"USD": -2400}
    else:
        assert sent == {"USD": -2400} and third == {}
    assert _verify(p, iid, cap).problems == []
