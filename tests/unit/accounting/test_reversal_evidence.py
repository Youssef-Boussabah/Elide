"""P1 review findings 5 and 7: a local correction is not provider proof of a refund, and
the settlement books keep legitimate non-zero balances (revenue, fees, FX position)."""

from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest

from api.accounting.journal import (
    EntryKind,
    EvidenceClass,
    JournalEntry,
    JournalError,
    Posting,
    ProviderEventRef,
)
from api.accounting.settlement_postings import settlement_entries
from api.domain.money import CAD, USD, SignedAmount
from api.integrations.paypal.settlement import load_settlement

RUNS = Path(__file__).resolve().parents[3] / "evidence" / "paypal-sandbox" / "runs"
REFUND = ProviderEventRef("paypal", "sandbox", "refund", "TEST-ONLY-NOT-A-REAL-REFUND")


@pytest.fixture
def entries():
    s = load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")
    return settlement_entries(s, "paypal_sandbox_merchant")


@pytest.fixture
def capture(entries):
    return entries[-1]


def later(e, days=1):
    return e.occurred_at + timedelta(days=days)


def test_reversal_must_declare_its_own_evidence_class(capture):
    with pytest.raises(TypeError):
        capture.reversal("local:rev:1", later(capture), "typo")  # no silent inheritance


def test_local_correction_of_a_verified_payment_is_a_manual_adjustment(capture):
    rev = capture.reversal(
        "local:rev:1",
        later(capture),
        "booked to wrong entity",
        evidence_class=EvidenceClass.MANUAL_ADJUSTMENT,
    )
    assert rev.evidence_class is EvidenceClass.MANUAL_ADJUSTMENT
    assert rev.provider_ref is None  # the capture reference is NOT copied
    assert rev.reverses_key == capture.idempotency_key  # link to the original is kept
    assert "booked to wrong entity" in rev.description
    assert capture.evidence_class is EvidenceClass.SANDBOX_VERIFIED  # original untouched


def test_reversal_cannot_claim_provider_verification_without_a_provider_reversal(capture):
    with pytest.raises(JournalError):  # no provider resource at all
        capture.reversal(
            "local:rev:1", later(capture), "refund", evidence_class=EvidenceClass.SANDBOX_VERIFIED
        )
    with pytest.raises(JournalError):  # citing the original capture is not refund evidence
        capture.reversal(
            "local:rev:1",
            later(capture),
            "refund",
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            provider_ref=capture.provider_ref,
        )
    with pytest.raises(JournalError):  # another capture is not a refund either
        capture.reversal(
            "local:rev:1",
            later(capture),
            "refund",
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            provider_ref=ProviderEventRef("paypal", "sandbox", "capture", "OTHER"),
        )


def test_provider_verified_reversal_cites_the_refund_resource(capture):
    rev = capture.reversal(
        "paypal:sandbox:refund:test-only:reversal",
        later(capture),
        "provider refund",
        evidence_class=EvidenceClass.SANDBOX_VERIFIED,
        provider_ref=REFUND,
    )
    assert rev.provider_ref == REFUND
    assert rev.provider_ref != capture.provider_ref


def test_manual_or_attested_reversal_cannot_cite_a_provider_resource(capture):
    for cls in (EvidenceClass.MANUAL_ADJUSTMENT, EvidenceClass.MERCHANT_ATTESTED):
        with pytest.raises(JournalError):
            capture.reversal(
                "local:rev:1", later(capture), "x", evidence_class=cls, provider_ref=REFUND
            )


def test_synthetic_and_real_books_never_reverse_each_other(capture):
    with pytest.raises(JournalError):
        capture.reversal("local:rev:1", later(capture), "x", evidence_class=EvidenceClass.SYNTHETIC)
    syn = JournalEntry(
        "e1",
        "syn:t:1",
        EntryKind.SYNTHETIC_TRANSFER,
        EvidenceClass.SYNTHETIC,
        "t",
        capture.occurred_at,
        (Posting("cash:a", SignedAmount(1, USD)), Posting("equity:a", SignedAmount(-1, USD))),
    )
    with pytest.raises(JournalError):
        syn.reversal("syn:t:1:rev", later(syn), "x", evidence_class=EvidenceClass.MANUAL_ADJUSTMENT)
    assert (
        syn.reversal(
            "syn:t:1:rev", later(syn), "x", evidence_class=EvidenceClass.SYNTHETIC
        ).evidence_class
        is EvidenceClass.SYNTHETIC
    )


def test_reversal_reason_is_required(capture):
    with pytest.raises(JournalError):
        capture.reversal(
            "local:rev:1", later(capture), "  ", evidence_class=EvidenceClass.MANUAL_ADJUSTMENT
        )


def test_hand_built_reversal_cannot_carry_the_capture_as_refund_proof(capture):
    with pytest.raises(JournalError):
        JournalEntry(
            capture.entity_id,
            "local:rev:hand",
            EntryKind.REVERSAL,
            EvidenceClass.SANDBOX_VERIFIED,
            "hand-built",
            later(capture),
            tuple(Posting(p.account, -p.amount) for p in capture.postings),
            provider_ref=capture.provider_ref,
            reverses_key=capture.idempotency_key,
        )


def test_manual_adjustment_class_is_only_for_corrections(capture):
    with pytest.raises(JournalError):
        JournalEntry(
            "e1",
            "manual:1",
            EntryKind.SYNTHETIC_TRANSFER,
            EvidenceClass.MANUAL_ADJUSTMENT,
            "not a correction",
            capture.occurred_at,
            (Posting("cash:a", SignedAmount(1, USD)), Posting("equity:a", SignedAmount(-1, USD))),
        )


# --- finding 7: what the settlement books do and do not claim -----------------------


def test_settlement_leaves_exactly_the_expected_nonzero_balances(entries):
    bal = defaultdict(int)
    for e in entries:
        for x in e.postings:
            bal[(x.account, x.amount.currency)] += x.amount.cents
    nonzero = {k: v for k, v in bal.items() if v}
    # Revenue, discount, fee and the FX conversion position legitimately keep balances;
    # only the receivable and the USD clearing account close to zero.
    assert nonzero == {
        ("revenue:sales", USD): -100000,
        ("contra_revenue:early_payment_discount", USD): 1000,
        ("expense:paypal_fees", USD): 2901,
        ("fx_conversion_position", USD): 96099,
        ("fx_conversion_position", CAD): -130679,
        ("paypal_balance", CAD): 130679,
    }
    # One cash receipt: the USD-equivalent net is an FX position, not a second cash asset.
    cash = {k for k in nonzero if k[0] == "paypal_balance"}
    assert cash == {("paypal_balance", CAD)}


def test_invoice_entry_does_not_claim_a_revenue_recognition_determination(entries):
    issued = entries[0]
    assert "illustrative" in issued.description
    assert "recognition" in issued.description
