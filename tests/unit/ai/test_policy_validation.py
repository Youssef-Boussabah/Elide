"""Deterministic policy validation on the labelled set `policy-eval-v1` (no model)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC

import pytest

from api.ai.mock import ScriptedLLMAdapter
from api.ai.schemas import PolicyExtraction
from api.policy.service import (
    PolicyError,
    PolicyStore,
    apply_confirmed_policy,
    draft_policy,
)
from api.policy.validation import Status, locate, parse_explicit_date, validate_policy
from fixtures import ai_demo
from fixtures import planning as fx
from fixtures.ai_eval_cases import ADVERSARIAL, CASES, READY, ext, m

BY_ID = {c["id"]: c for c in CASES}


def run(case, extraction=None):
    return validate_policy(
        merchant_id="buyer",
        text=case["text"],
        source=case["source"],
        as_of=ai_demo.AS_OF,
        extraction=PolicyExtraction.model_validate(extraction or case["reference"]),
        context=ai_demo.merchant_context("buyer"),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_reference_extraction_gets_the_labelled_outcome(case):
    rep = run(case)
    assert rep.status.value in case["expected_status"], [i.code for i in rep.issues]
    if rep.status is Status.READY_FOR_CONFIRMATION:
        p = rep.proposed
        for key, want in case["expected_values"].items():
            if key == "valid_until_date":
                assert p.valid_until.startswith(want)
            elif key == "offer_deadline_date":
                assert p.offer_response_deadline.startswith(want)
            elif key == "prohibited":
                assert list(p.prohibited) == want
            else:
                assert getattr(p, key) == want, key


@pytest.mark.parametrize("adv", ADVERSARIAL, ids=lambda a: a["id"])
def test_compromised_model_output_is_never_ready(adv):
    rep = run(BY_ID[adv["case"]], adv["extraction"])
    assert rep.status is not Status.READY_FOR_CONFIRMATION, adv["why"]


def test_spans_point_into_the_original_text():
    c = BY_ID["ord-10"]
    rep = run(c)
    start, end, quote = rep.proposed.spans["reserve_floor"]
    assert c["text"][start:end] == quote == "Keep $1,250.50 in reserve"
    assert locate("Keep  $600\nnow", "keep $600 now") == (0, 14)


def test_missing_threshold_stays_missing_not_zero():
    rep = run(BY_ID["ord-02"])
    assert rep.proposed.fee_ceiling_cents is None
    assert rep.proposed.max_early_commitment_cents is None
    assert rep.proposed.reserve_floor_cents == 50000  # unchanged confirmed value


def test_explicit_dates_only():
    assert str(parse_explicit_date("2026-11-20")) == "2026-11-20"
    assert str(parse_explicit_date("Nov 20, 2026")) == "2026-11-20"
    assert str(parse_explicit_date("20 November 2026")) == "2026-11-20"
    for phrase in ("Friday", "next week", "11/12", "end of month", "tomorrow", "2026-02-30"):
        assert parse_explicit_date(phrase) is None


def test_relaxation_is_refused_but_tightening_is_allowed():
    assert run(BY_ID["rel-01"]).status is Status.REJECTED
    rep = run(BY_ID["ord-01"])
    assert rep.status is Status.READY_FOR_CONFIRMATION
    assert {"field": "reserve_floor", "from": 50000, "to": 60000, "kind": "tighten"} in rep.changes


def test_document_text_cannot_set_policy():
    c = BY_ID["inj-01"]
    rep = run(c, ext(fee_ceiling=m("1,000.00", "Amount due: $1,000.00")))
    assert rep.status is Status.REJECTED
    assert "DOCUMENT_CANNOT_SET_POLICY" in [i.code for i in rep.issues]


def test_scanner_flags_attempts_whatever_the_model_says():
    c = BY_ID["inj-01"]
    rep = run(c, ext())  # model reports nothing at all
    codes = {i.code for i in rep.issues}
    assert {"OVERRIDE_ATTEMPT", "PAYMENT_AUTHORIZATION_REQUEST", "IMPERSONATION_REQUEST"} <= codes
    assert rep.status is not Status.READY_FOR_CONFIRMATION


# --- drafting, confirmation and application ------------------------------------------
def _adapter_for(case):
    return ScriptedLLMAdapter(lambda name, s, u: case["reference"])


def _draft(case_id):
    c = BY_ID[case_id]
    return draft_policy(
        _adapter_for(c),
        merchant_id="buyer",
        text=c["text"],
        source=c["source"],
        as_of=ai_demo.AS_OF,
        context=ai_demo.merchant_context("buyer"),
    )


def test_draft_is_labelled_mock_and_never_active():
    d = _draft("ord-01")
    assert d.model_call.mocked is True
    assert d.model_call.as_dict()["label"] == "MOCK_MODEL_OUTPUT"
    assert d.status == READY
    store = PolicyStore()
    store.save(d)
    assert store.active == {}  # saving a draft activates nothing


def test_only_the_same_merchant_with_the_exact_digest_can_confirm():
    store = PolicyStore()
    d = store.save(_draft("ord-01"))
    with pytest.raises(PolicyError):
        store.confirm(
            d.draft_id, merchant_id="food_supplier", digest=d.report.digest, at=ai_demo.AS_OF
        )
    with pytest.raises(PolicyError):
        store.confirm(d.draft_id, merchant_id="buyer", digest="0" * 64, at=ai_demo.AS_OF)
    c = store.confirm(d.draft_id, merchant_id="buyer", digest=d.report.digest, at=ai_demo.AS_OF)
    assert c.version == 1 and store.active["buyer"] is c


def test_a_draft_needing_clarification_cannot_be_confirmed():
    store = PolicyStore()
    d = store.save(_draft("dat-01"))
    with pytest.raises(PolicyError):
        store.confirm(
            d.draft_id, merchant_id="buyer", digest=d.report.digest or "", at=ai_demo.AS_OF
        )


def test_confirmed_policy_tightens_the_plan_and_drafts_cannot_be_applied():
    store = PolicyStore()
    d = store.save(_draft("ord-01"))  # reserve $600 instead of $500
    with pytest.raises(PolicyError):
        apply_confirmed_policy(fx.network_two_candidates(), d, ai_demo.AS_OF)
    c = store.confirm(d.draft_id, merchant_id="buyer", digest=d.report.digest, at=ai_demo.AS_OF)
    net = apply_confirmed_policy(fx.network_two_candidates(), c, ai_demo.AS_OF)
    assert net.position("buyer").protected_reserve.cents == 60000
    from api.planning.solver import SolveConfig, solve

    assert solve(net, SolveConfig()).schedule == ()  # $990 no longer fits above a $600 reserve
    assert any(label.startswith("POLICY_APPLIED:buyer") for label in net.labels)


def test_prohibition_and_fee_ceiling_apply_to_the_planner():
    store = PolicyStore()
    d = store.save(_draft("ord-04"))
    c = store.confirm(d.draft_id, merchant_id="buyer", digest=d.report.digest, at=ai_demo.AS_OF)
    net = apply_confirmed_policy(fx.network_two_candidates(), c, ai_demo.AS_OF)
    assert "packaging_supplier" in net.policy("buyer").prohibited_counterparties


def test_expired_policy_is_not_applied():
    store = PolicyStore()
    d = store.save(_draft("ord-08"))  # valid until 2026-11-30
    c = store.confirm(d.draft_id, merchant_id="buyer", digest=d.report.digest, at=ai_demo.AS_OF)
    from datetime import datetime

    later = datetime(2026, 12, 5, tzinfo=UTC)
    assert (
        apply_confirmed_policy(fx.network_two_candidates(), c, later) == fx.network_two_candidates()
    )


def test_unconfigured_model_falls_back_to_manual_entry():
    from api.ai.adapter import UnconfiguredLLMAdapter

    c = BY_ID["ord-01"]
    d = draft_policy(
        UnconfiguredLLMAdapter(),
        merchant_id="buyer",
        text=c["text"],
        source="merchant_message",
        as_of=ai_demo.AS_OF,
        context=ai_demo.merchant_context("buyer"),
    )
    assert d.status == "MODEL_UNAVAILABLE" and d.report is None
    assert "manually" in d.message


def test_schema_invalid_model_output_is_reported_not_guessed():
    c = BY_ID["ord-01"]
    bad = ScriptedLLMAdapter(lambda *a: {"reserve_floor": 600})
    d = draft_policy(
        bad,
        merchant_id="buyer",
        text=c["text"],
        source="merchant_message",
        as_of=ai_demo.AS_OF,
        context=ai_demo.merchant_context("buyer"),
    )
    assert d.status == "MODEL_OUTPUT_INVALID"


def test_counterparty_scope_is_the_merchants_own_network():
    ctx = ai_demo.merchant_context("buyer")
    assert "mechanic" not in ctx.counterparties.values()
    other = replace(ctx, merchant_id="carrier")
    rep = validate_policy(
        merchant_id="buyer",
        text="Keep $600 reserved.",
        source="merchant_message",
        as_of=ai_demo.AS_OF,
        extraction=PolicyExtraction.model_validate(BY_ID["ord-01"]["reference"]),
        context=other,
    )
    assert rep.status is Status.REJECTED


def test_uncaptured_date_phrase_needs_clarification():
    c = BY_ID["dat-01"]
    rep = run(c, ext(reserve_floor=m("600", "Keep $600 for payroll through Friday.")))
    assert rep.status is Status.NEEDS_CLARIFICATION
    assert "UNCAPTURED_DATE" in [i.code for i in rep.issues]
    assert run(BY_ID["ord-08"]).status is Status.READY_FOR_CONFIRMATION  # captured date is fine


def test_unrequested_approval_preference_is_ignored_not_applied():
    c = BY_ID["ord-01"]
    rep = run(c, {**c["reference"], "approval_preference": "approve_each_payment"})
    assert rep.status is Status.READY_FOR_CONFIRMATION
    assert rep.proposed.approval_preference == "unspecified"
    assert "UNGROUNDED_PREFERENCE_IGNORED" in [i.code for i in rep.issues]
