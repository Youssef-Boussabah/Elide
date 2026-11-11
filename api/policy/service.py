"""Policy drafting, confirmation and application to planning (deterministic around the model).

- `draft_policy`: model extraction (untrusted, may be MOCK) + `validate_policy`. Returns a
  `PolicyDraft` that is never active.
- `PolicyStore.confirm`: the only way to activate a policy. Requires the same merchant, a
  READY draft and the exact digest the merchant reviewed. In P3 this is a local,
  synthetic demo confirmation with no authentication (P4 adds sessions/step-up), so it is
  NOT exposed over HTTP.
- `apply_confirmed_policy`: accepts only a `ConfirmedPolicy` and returns a new planning
  `Network`; it never touches invoices, payments or stored cash commitments.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime

from api.ai.adapter import LLMAdapter, LLMOutputError, LLMUnavailableError, ModelCall
from api.ai.prompts import POLICY_PROMPT_VERSION, POLICY_SYSTEM, policy_user
from api.ai.schemas import PolicyExtraction
from api.domain.money import USD, Money
from api.planning.network import Network
from api.policy.validation import (
    CurrentPolicy,
    MerchantContext,
    ProposedPolicy,
    Source,
    Status,
    ValidationReport,
    validate_policy,
)

MAX_TEXT_CHARS = 2000


class PolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PolicyDraft:
    draft_id: str
    merchant_id: str
    source: Source
    text: str
    as_of: datetime
    outcome: str  # VALIDATED / MODEL_UNAVAILABLE / MODEL_OUTPUT_INVALID
    extraction: PolicyExtraction | None
    model_call: ModelCall | None
    report: ValidationReport | None
    message: str = ""

    @property
    def status(self) -> str:
        return self.report.status.value if self.report else self.outcome


def draft_policy(
    adapter: LLMAdapter,
    *,
    merchant_id: str,
    text: str,
    source: Source,
    as_of: datetime,
    context: MerchantContext,
) -> PolicyDraft:
    if not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise PolicyError(f"instruction must be 1-{MAX_TEXT_CHARS} characters")
    base = {"merchant_id": merchant_id, "source": source, "text": text, "as_of": as_of.isoformat()}
    try:
        res = adapter.generate_structured(
            system=POLICY_SYSTEM,
            user=policy_user(text),
            schema=PolicyExtraction,
            schema_name="policy_extraction",
            prompt_version=POLICY_PROMPT_VERSION,
            max_tokens=1500,
        )
    except LLMOutputError as exc:
        return PolicyDraft(
            _id(base, "invalid"),
            merchant_id,
            source,
            text,
            as_of,
            "MODEL_OUTPUT_INVALID",
            None,
            None,
            None,
            f"{exc}; enter the fields manually and validate them",
        )
    except LLMUnavailableError as exc:
        return PolicyDraft(
            _id(base, "unavailable"),
            merchant_id,
            source,
            text,
            as_of,
            "MODEL_UNAVAILABLE",
            None,
            None,
            None,
            f"{exc}; enter the fields manually and validate them",
        )
    report = validate_policy(
        merchant_id=merchant_id,
        text=text,
        source=source,
        as_of=as_of,
        extraction=res.value,
        context=context,
    )
    return PolicyDraft(
        _id(base, res.value.model_dump_json()),
        merchant_id,
        source,
        text,
        as_of,
        "VALIDATED",
        res.value,
        res.call,
        report,
    )


def _id(base: dict, salt: str) -> str:
    blob = json.dumps({**base, "salt": salt}, sort_keys=True)
    return "pdr_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ConfirmedPolicy:
    merchant_id: str
    version: int
    policy: ProposedPolicy
    digest: str
    draft_id: str
    confirmed_at: datetime
    basis: str = "synthetic_demo_confirmation"  # P4: authenticated merchant decision


@dataclass
class PolicyStore:
    drafts: dict[str, PolicyDraft] = field(default_factory=dict)
    active: dict[str, ConfirmedPolicy] = field(default_factory=dict)

    def save(self, draft: PolicyDraft) -> PolicyDraft:
        self.drafts[draft.draft_id] = draft
        return draft

    def confirm(
        self, draft_id: str, *, merchant_id: str, digest: str, at: datetime
    ) -> ConfirmedPolicy:
        draft = self.drafts.get(draft_id)
        if draft is None or draft.merchant_id != merchant_id:
            raise PolicyError("no such draft for this merchant")  # same answer either way
        report = draft.report
        if report is None or report.status is not Status.READY_FOR_CONFIRMATION:
            raise PolicyError(f"draft is {draft.status}; only a READY draft can be confirmed")
        if report.digest != digest:
            raise PolicyError("digest does not match the reviewed draft")
        prev = self.active.get(merchant_id)
        if report.proposed is None:
            raise PolicyError("draft has no proposed policy")
        confirmed = ConfirmedPolicy(
            merchant_id, (prev.version + 1) if prev else 1, report.proposed, digest, draft_id, at
        )
        self.active[merchant_id] = confirmed
        return confirmed


def current_from(confirmed: ConfirmedPolicy | None) -> CurrentPolicy:
    if confirmed is None:
        return CurrentPolicy()
    p = confirmed.policy
    return CurrentPolicy(
        reserve_floor_cents=p.reserve_floor_cents,
        max_early_commitment_cents=p.max_early_commitment_cents,
        min_early_discount_cents=p.min_early_discount_cents,
        min_net_benefit_cents=p.min_net_benefit_cents,
        fee_ceiling_cents=p.fee_ceiling_cents,
        allowed=p.allowed,
        prohibited=p.prohibited,
        participation=p.participation,
        approval_preference=p.approval_preference,
    )


def apply_confirmed_policy(net: Network, confirmed: ConfirmedPolicy, at: datetime) -> Network:
    """Planning inputs under a confirmed policy (only ever tighter than before).

    - reserve: max(evidence reserve, policy floor), and, with a maximum early commitment,
      raised so at most that much opening cash can be deployed (conservative: it also
      limits on-time payments funded from opening cash);
    - minimum discount / net benefit: the stricter of policy and existing values;
    - fee ceiling: the lower; prohibited counterparties: union; an allowed list prohibits
      every other counterparty; opt-out stops the merchant participating.
    An expired policy (valid_until passed) is not applied.
    """
    if not isinstance(confirmed, ConfirmedPolicy):
        raise PolicyError("only a ConfirmedPolicy can change planning inputs")
    p = confirmed.policy
    m = confirmed.merchant_id
    if p.valid_until is not None and datetime.fromisoformat(p.valid_until) < at:
        return net
    pos = net.position(m)
    reserve = pos.protected_reserve.cents
    if p.reserve_floor_cents is not None:
        reserve = max(reserve, p.reserve_floor_cents)
    if p.max_early_commitment_cents is not None:
        reserve = max(
            reserve, pos.available.cents - pos.committed.cents - p.max_early_commitment_cents
        )
    new_pos = replace(pos, protected_reserve=Money(reserve, USD))
    pol = net.policy(m)
    counterparties = {
        o.creditor if o.debtor == m else o.debtor
        for o in net.obligations
        if m in (o.debtor, o.creditor)
    }
    prohibited = set(pol.prohibited_counterparties) | set(p.prohibited)
    if p.allowed:
        prohibited |= counterparties - set(p.allowed)
    fee = pol.max_fee_per_leg
    if p.fee_ceiling_cents is not None:
        ceiling = Money(p.fee_ceiling_cents, USD)
        fee = ceiling if fee is None or ceiling < fee else fee
    new_pol = replace(
        pol,
        participating=pol.participating and p.participation != "opt_out",
        min_early_discount=Money(
            max(pol.min_early_discount.cents, p.min_early_discount_cents or 0), USD
        ),
        min_net_benefit_cents=max(pol.min_net_benefit_cents, p.min_net_benefit_cents or 0),
        max_fee_per_leg=fee,
        prohibited_counterparties=frozenset(prohibited),
        source=f"confirmed policy v{confirmed.version} ({confirmed.basis})",
    )
    return replace(
        net,
        positions=tuple(new_pos if x.merchant_id == m else x for x in net.positions),
        policies=tuple(new_pol if x.merchant_id == m else x for x in net.policies),
        labels=(*net.labels, f"POLICY_APPLIED:{m}:v{confirmed.version}"),
    )
