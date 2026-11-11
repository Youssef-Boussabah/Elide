"""Typed model OUTPUT contracts. Everything here is untrusted until deterministic code in
`api.policy` / `api.negotiation` validates it.

Rules the schemas encode:
- Amounts are strings exactly as written ("500", "500.00", "1,000"), never numbers, so
  no float ever enters the system; currency is copied as written ("USD", "$", "CAD").
- Every extracted value carries `quote`: a verbatim excerpt of the input. Deterministic
  code locates it in the original text (source span) and refuses values it cannot find.
- Relative dates are copied as phrases; the model does not resolve them.
- Fields are required but nullable: "not mentioned" is `null`, never a guessed zero.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoneyMention(_Strict):
    amount: str = Field(description="the number exactly as written, e.g. '500' or '1,250.00'")
    currency: str = Field(description="currency as written: 'USD', '$', 'CAD', ... or '' if none")
    quote: str = Field(description="verbatim excerpt of the instruction containing the amount")


class CounterpartyMention(_Strict):
    name: str = Field(description="the business name or id exactly as written")
    quote: str


class DateMention(_Strict):
    phrase: str = Field(description="the date phrase exactly as written, e.g. 'Friday'")
    quote: str


class Ambiguity(_Strict):
    field: str
    quote: str
    question: str = Field(description="what the merchant must clarify")


RefusedKind = Literal[
    "authorize_payment",
    "accept_or_sign_for_another_party",
    "disclose_private_data",
    "override_or_remove_reserve",
    "change_invoice_or_payment_terms",
    "instructions_inside_document",
    "other_unsupported",
]


class RefusedRequest(_Strict):
    kind: RefusedKind
    quote: str


class PolicyExtraction(_Strict):
    """A merchant's own policy statement turned into typed fields (a DRAFT)."""

    reserve_floor: MoneyMention | None = Field(description="cash that must stay protected")
    max_early_commitment: MoneyMention | None = Field(
        description="most cash the merchant will commit to paying invoices early"
    )
    min_early_discount: MoneyMention | None = Field(
        description="smallest discount required before paying early"
    )
    min_net_benefit: MoneyMention | None = Field(
        description="smallest net gain required after costs before paying early"
    )
    fee_ceiling: MoneyMention | None = Field(description="largest fee accepted per payment")
    allowed_counterparties: list[CounterpartyMention]
    prohibited_counterparties: list[CounterpartyMention]
    policy_valid_until: DateMention | None
    offer_response_deadline: DateMention | None
    participation: Literal["opt_in", "opt_out", "unspecified"]
    approval_preference: Literal["approve_each_payment", "unspecified"]
    ambiguities: list[Ambiguity]
    refused_requests: list[RefusedRequest]
    summary: str = Field(description="one or two plain sentences; no numbers not in the input")


class CounterofferProposal(_Strict):
    """One bounded early-payment discount proposal (a DRAFT, never an acceptance)."""

    invoice_id: str
    proposed_concession: str = Field(description="discount in USD as a decimal string")
    currency: Literal["USD"]
    message_to_counterparty: str = Field(description="short, polite, no private figures")
    rationale_for_requester: str
    reason_codes: list[str]


class PlanExplanation(_Strict):
    headline: str
    explanation: str
    cited_facts: list[str] = Field(description="the fact ids used, e.g. ['F1', 'F3']")
