"""Pure journal model and the Sandbox settlement postings (no database)."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from api.accounting.journal import (
    EntryKind,
    EvidenceClass,
    JournalEntry,
    JournalError,
    Posting,
    ProviderEventRef,
    UnbalancedEntryError,
)
from api.accounting.settlement_postings import settlement_entries
from api.domain.money import CAD, USD, SignedAmount
from api.integrations.paypal.settlement import load_settlement

T0 = datetime(2026, 11, 10, 15, tzinfo=UTC)
RUNS = Path(__file__).resolve().parents[3] / "evidence" / "paypal-sandbox" / "runs"


def entry(postings, **kw):
    base = {
        "entity_id": "e1",
        "idempotency_key": "syn:test:1",
        "kind": EntryKind.SYNTHETIC_TRANSFER,
        "evidence_class": EvidenceClass.SYNTHETIC,
        "description": "test",
        "occurred_at": T0,
        "postings": tuple(postings),
    }
    base.update(kw)
    return JournalEntry(**base)


def p(account, cents, cur=USD):
    return Posting(account, SignedAmount(cents, cur))


def test_balanced_entry_accepted():
    e = entry([p("cash:a", 100), p("equity:a", -100)])
    assert e.totals_by_currency() == {USD: 0}


def test_unbalanced_and_single_line_entries_rejected():
    with pytest.raises(UnbalancedEntryError):
        entry([p("cash:a", 100), p("equity:a", -99)])
    with pytest.raises(UnbalancedEntryError):
        entry([p("cash:a", 100)])


def test_each_currency_must_balance_on_its_own():
    """USD debit cannot be 'balanced' by a CAD credit of the same number."""
    with pytest.raises(UnbalancedEntryError):
        entry([p("cash:a", 100, USD), p("fx:a", -100, CAD)])
    ok = entry(
        [p("fx:a", 100, USD), p("cash:a", -100, USD), p("cash:b", 136, CAD), p("fx:a", -136, CAD)]
    )
    assert ok.totals_by_currency() == {USD: 0, CAD: 0}


@pytest.mark.parametrize("bad", ["", "Cash", "cash::x", "1cash", "cash:a b"])
def test_bad_account_names_rejected(bad):
    with pytest.raises(JournalError):
        Posting(bad, SignedAmount(1, USD))


def test_zero_or_float_postings_impossible():
    with pytest.raises(ValueError):
        Posting("cash:a", SignedAmount(0, USD))
    with pytest.raises(ValueError):
        Posting("cash:a", SignedAmount(1.0, USD))


def test_evidence_class_rules():
    ref = ProviderEventRef("paypal", "sandbox", "capture", "X")
    with pytest.raises(JournalError):  # provider-verified without a provider resource
        entry([p("a:x", 1), p("b:x", -1)], evidence_class=EvidenceClass.SANDBOX_VERIFIED)
    with pytest.raises(JournalError):  # synthetic data may not cite a provider resource
        entry([p("a:x", 1), p("b:x", -1)], provider_ref=ref)
    with pytest.raises(JournalError):
        ProviderEventRef("paypal", "live", "capture", "X")


def test_reversal_is_exact_negation_and_cannot_predate_or_chain():
    e = entry([p("cash:a", 100), p("equity:a", -100)])
    r = e.reversal("syn:test:1:rev", T0 + timedelta(hours=1), "refund")
    assert [(x.account, x.amount.cents) for x in r.postings] == [
        ("cash:a", -100),
        ("equity:a", 100),
    ]
    assert r.reverses_key == e.idempotency_key and r.kind is EntryKind.REVERSAL
    with pytest.raises(JournalError):
        e.reversal("syn:test:1:rev2", T0 - timedelta(seconds=1), "backdated")
    with pytest.raises(JournalError):
        r.reversal("syn:test:1:rev:rev", T0 + timedelta(hours=2), "chain")


def test_digest_changes_with_content_not_with_timezone_spelling():
    e1 = entry([p("cash:a", 100), p("equity:a", -100)])
    e2 = entry([p("cash:a", 100), p("equity:a", -100)], occurred_at=T0.astimezone())
    e3 = entry([p("cash:a", 101), p("equity:a", -101)])
    assert e1.content_digest() == e2.content_digest()
    assert e1.content_digest() != e3.content_digest()


@given(
    st.lists(
        st.tuples(st.integers(-(10**9), 10**9).filter(bool), st.sampled_from([USD, CAD])),
        min_size=1,
        max_size=8,
    )
)
def test_any_entry_closed_by_per_currency_balancing_lines_is_accepted(lines):
    postings = [p(f"acct:{i}", c, cur) for i, (c, cur) in enumerate(lines)]
    totals = defaultdict(int)
    for c, cur in lines:
        totals[cur] += c
    for cur, total in totals.items():
        if total:
            postings.append(p(f"balance:{cur.lower()}", -total, cur))
    if len(postings) < 2 or any(
        sum(x.amount.cents for x in postings if x.amount.currency == c) for c in totals
    ):
        return
    e = entry(postings)
    assert all(v == 0 for v in e.totals_by_currency().values())
    # Removing any single line always breaks the balance.
    for i in range(len(postings)):
        rest = postings[:i] + postings[i + 1 :]
        if len(rest) >= 2:
            with pytest.raises(UnbalancedEntryError):
                entry(rest)


# --- real Sandbox settlement ----------------------------------------------------------


@pytest.fixture
def entries():
    s = load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")
    return settlement_entries(s, "paypal_sandbox_merchant")


def test_settlement_produces_three_keyed_provider_entries(entries):
    assert [e.kind for e in entries] == [
        EntryKind.INVOICE_ISSUED,
        EntryKind.CONCESSION_AUTHORIZED,
        EntryKind.PROVIDER_SETTLEMENT,
    ]
    assert [e.idempotency_key for e in entries] == [
        "paypal:sandbox:invoice:inv2-4ruv-xpeh-4f3c-t8bw:issued",
        "paypal:sandbox:invoice:inv2-4ruv-xpeh-4f3c-t8bw:concession",
        "paypal:sandbox:capture:4j016440mv5285528:settlement",
    ]
    assert all(e.evidence_class is EvidenceClass.SANDBOX_VERIFIED for e in entries)
    assert entries[-1].provider_ref == ProviderEventRef(
        "paypal", "sandbox", "capture", "4J016440MV5285528"
    )


def test_settlement_entry_dates_use_undisputed_provider_times(entries):
    issued, _, capture = entries
    assert issued.occurred_at == datetime(2026, 11, 10, 21, 45, 18, tzinfo=UTC)
    assert capture.occurred_at == datetime(2026, 11, 10, 22, 15, 48, tzinfo=UTC)  # not 14:15:49


def test_settlement_books_one_cash_receipt_and_the_fee(entries):
    bal = defaultdict(int)
    for e in entries:
        for x in e.postings:
            bal[(x.account, x.amount.currency)] += x.amount.cents
    cash_like = {
        k: v
        for k, v in bal.items()
        if k[0] in ("paypal_balance", "paypal_settlement_clearing") and v
    }
    assert cash_like == {("paypal_balance", CAD): 130679}
    assert bal[("expense:paypal_fees", USD)] == 2901
    assert bal[("accounts_receivable:INV2-4RUV-XPEH-4F3C-T8BW", USD)] == 0
    assert bal[("contra_revenue:early_payment_discount", USD)] == 1000
