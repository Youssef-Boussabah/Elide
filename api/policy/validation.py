"""Deterministic validation of a model-extracted policy draft. No model calls here.

The model is never the authority. This module decides, from the merchant's original text,
the merchant's context and its currently confirmed policy:

- grounding: every value must be backed by a verbatim quote found in the text, and an
  amount must literally appear in its quote (a model-invented number is refused);
- money: exact decimal strings only, USD only (CAD, EUR, ... are refused), within bounds;
- dates: only explicit calendar dates ("2026-11-20", "November 20, 2026") are accepted,
  resolved in the merchant's business time zone; relative dates ("Friday") must be
  clarified, whatever the model "resolved";
- scope: counterparties must be the merchant's own known counterparties;
- no relaxation: an AI draft may add or tighten restrictions, never loosen a confirmed
  one (lower reserve, higher cap or fee ceiling, lower minimum, opening a prohibited
  counterparty, opting in). Loosening needs a direct merchant edit (P4);
- documents cannot set policy: pasted invoice/email text is data;
- an independent scanner flags override, payment-authorization, impersonation and
  disclosure attempts in the raw text, whatever the model reported;
- a missing threshold stays missing (never zero).

Outcome: REJECTED (any blocking issue), NEEDS_CLARIFICATION (any question), or
READY_FOR_CONFIRMATION. Even READY is only a draft: nothing applies until `confirm`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from api.ai.schemas import CounterpartyMention, DateMention, MoneyMention, PolicyExtraction

MAX_CENTS = {
    "reserve_floor": 1_000_000_000,  # USD 10,000,000.00
    "max_early_commitment": 1_000_000_000,
    "min_early_discount": 100_000_000,
    "min_net_benefit": 100_000_000,
    "fee_ceiling": 10_000_000,
}
MONEY_FIELDS = tuple(MAX_CENTS)
Source = Literal["merchant_message", "document_text"]


class Status(StrEnum):
    READY_FOR_CONFIRMATION = "READY_FOR_CONFIRMATION"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Literal["block", "clarify", "info"]
    field: str | None
    message: str
    span: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class CurrentPolicy:
    """The merchant's confirmed restrictions the draft is compared against."""

    reserve_floor_cents: int | None = None
    max_early_commitment_cents: int | None = None
    min_early_discount_cents: int | None = None
    min_net_benefit_cents: int | None = None
    fee_ceiling_cents: int | None = None
    allowed: tuple[str, ...] = ()
    prohibited: tuple[str, ...] = ()
    participation: str = "unspecified"
    approval_preference: str = "unspecified"


@dataclass(frozen=True, slots=True)
class MerchantContext:
    merchant_id: str
    business_tz: str
    counterparties: dict[str, str]  # lower-case alias -> merchant id (own network only)
    current: CurrentPolicy = field(default_factory=CurrentPolicy)
    evidence_reserve_cents: int | None = None
    committed_cents: int = 0


@dataclass(frozen=True, slots=True)
class ProposedPolicy:
    merchant_id: str
    reserve_floor_cents: int | None
    max_early_commitment_cents: int | None
    min_early_discount_cents: int | None
    min_net_benefit_cents: int | None
    fee_ceiling_cents: int | None
    allowed: tuple[str, ...]
    prohibited: tuple[str, ...]
    valid_until: str | None
    offer_response_deadline: str | None
    participation: str
    approval_preference: str
    spans: dict[str, tuple[int, int, str]]

    def digest(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=list)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationReport:
    status: Status
    issues: tuple[Issue, ...]
    proposed: ProposedPolicy | None
    changes: tuple[dict[str, object], ...]
    explanation: str

    @property
    def digest(self) -> str | None:
        return self.proposed.digest() if self.proposed else None


# --- text helpers ----------------------------------------------------------------------
_WS = re.compile(r"\s+")
_AMOUNT = re.compile(r"^\$?\s*((?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.(\d{1,2}))?$")
_NUMBER_IN_TEXT = re.compile(r"(?<![\d.,])((?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.(\d{1,2}))?(?![\d,])")
_USD = {"$", "usd", "us$", "dollar", "dollars", "us dollars", "usd$"}
_FOREIGN = re.compile(
    r"(\bCAD\b|\bC\$|\bCA\$|\bEUR\b|€|£|\bGBP\b|\bMXN\b|\bJPY\b|¥|\bAUD\b|\bA\$|\beuros?\b|\bpounds?\b|\bcanadian\b|\bpesos?\b|\byen\b)",
    re.I,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9


_OPT_IN = re.compile(r"\b(opt(?:ing)?(?:[- ](?:me|us))?[- ]?in|join|participate|enrol+)\b", re.I)
_OPT_OUT = re.compile(
    r"\b(opt(?:ing)?(?:[- ](?:me|us))?[- ]?out|withdraw|leave|stop participating|exclude me|"
    r"do not participate|don't participate|not participate)\b",
    re.I,
)
_DATE_LIKE = re.compile(
    r"\b(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|tomorrow|today|"
    r"tonight|next (?:week|month|year)|end of (?:the )?(?:day|week|month|quarter|year)|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?"
    r"(?:,? \d{4})?|\d{1,2}(?:st|nd|rd|th)? (?:of )?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|"
    r"oct|nov|dec)[a-z]*(?:,? \d{4})?)\b",
    re.I,
)
_APPROVE_EACH = re.compile(r"\bapprov\w*\b[^.\n]{0,30}\b(every|each|all)\b", re.I)


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def locate(text: str, quote: str) -> tuple[int, int] | None:
    """Find `quote` in `text` (whitespace-normalised, case-insensitive) -> char span."""
    q = _norm(quote).casefold()
    if not q:
        return None
    # map normalised positions back to the original text
    chars, index = [], []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space or not chars:
                continue
            chars.append(" ")
            index.append(i)
            prev_space = True
        else:
            chars.append(ch.casefold())
            index.append(i)
            prev_space = False
    hay = "".join(chars)
    pos = hay.find(q)
    if pos < 0:
        return None
    return index[pos], index[pos + len(q) - 1] + 1


def _cents(amount: str) -> int | None:
    m = _AMOUNT.match(amount.strip())
    if not m:
        return None
    whole = int(m.group(1).replace(",", ""))
    frac = (m.group(2) or "").ljust(2, "0")
    return whole * 100 + int(frac or 0)


def _numbers_in(quote: str) -> set[int]:
    out = set()
    for m in _NUMBER_IN_TEXT.finditer(quote):
        whole = int(m.group(1).replace(",", ""))
        frac = (m.group(2) or "").ljust(2, "0")
        out.add(whole * 100 + int(frac or 0))
    return out


def parse_explicit_date(phrase: str) -> date | None:
    p = _norm(phrase).rstrip(".").casefold()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", p)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})", p)
    if m and m[1] in _MONTHS:
        try:
            return date(int(m[3]), _MONTHS[m[1]], int(m[2]))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)\.?,? (\d{4})", p)
    if m and m[2] in _MONTHS:
        try:
            return date(int(m[3]), _MONTHS[m[2]], int(m[1]))
        except ValueError:
            return None
    return None


# --- independent scanner of the raw text --------------------------------------------
SCANNER: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "OVERRIDE_ATTEMPT",
        "clarify",
        re.compile(
            r"\b(ignore|disregard|override|bypass|forget|skip)\b[^.\n]{0,60}"
            r"\b(reserve|rule|rules|policy|polic(?:y|ies)|instruction|instructions|limit|limits|previous|above|safeguard)",
            re.I,
        ),
    ),
    (
        "OVERRIDE_ATTEMPT",
        "clarify",
        re.compile(
            r"(system prompt|you are now|new instructions|developer mode|act as"
            r"|<\s*/?\s*(system|instruction)\s*>)",
            re.I,
        ),
    ),
    (
        "PAYMENT_AUTHORIZATION_REQUEST",
        "info",
        re.compile(
            r"\b(authori[sz]e|approve|execute|send|make|release|initiate)\b[^.\n]{0,30}\b(payment|payments|transfer|funds)\b"
            r"|\bpay\b[^.\n]{0,30}\b(now|immediately|today|right away|asap)\b",
            re.I,
        ),
    ),
    (
        "IMPERSONATION_REQUEST",
        "info",
        re.compile(
            r"\bon behalf of\b|\bas the (supplier|buyer|carrier|vendor|seller)\b"
            r"|\b(supplier|vendor|seller|carrier|buyer|they)\b[^.\n]{0,25}\b(accepted|agreed|approved|signed)\b"
            r"|\bmark\b[^.\n]{0,40}\b(accepted|approved|agreed|signed|paid|available|verified)\b",
            re.I,
        ),
    ),
    (
        "DISCLOSURE_REQUEST",
        "info",
        re.compile(
            r"\b(show|tell|reveal|share|disclose|send|give)\b[^.\n]{0,50}"
            r"\b(their|supplier'?s?|buyer'?s?|carrier'?s?|other|others'?|competitor'?s?)\b[^.\n]{0,40}"
            r"\b(reserve|balance|threshold|minimum|policy|cash|limit|margin)",
            re.I,
        ),
    ),
    (
        "BALANCE_CLAIM_IGNORED",
        "info",
        re.compile(r"\b(i|we)\s*(have|hold|got|'ve got|ve got)\b[^.\n]{0,25}\$?\s?\d", re.I),
    ),
)
_SCANNER_MESSAGES = {
    "OVERRIDE_ATTEMPT": "the text contains an instruction to ignore or override rules; it was "
    "not followed. Please confirm what you want in your own words",
    "PAYMENT_AUTHORIZATION_REQUEST": "a request to make or authorize a payment was ignored: "
    "policies never authorize payments (each payment needs its own approval)",
    "IMPERSONATION_REQUEST": "a request to accept, sign or mark something for another party was "
    "ignored: only that party can approve its own terms",
    "DISCLOSURE_REQUEST": "a request for another merchant's private data was ignored",
    "BALANCE_CLAIM_IGNORED": "balances come only from verified capacity evidence, not from "
    "policy text",
}


# --- validation ------------------------------------------------------------------------
def validate_policy(
    *,
    merchant_id: str,
    text: str,
    source: Source,
    as_of: datetime,
    extraction: PolicyExtraction,
    context: MerchantContext,
) -> ValidationReport:
    issues: list[Issue] = []

    def add(code, severity, fld, message, span=None):
        issues.append(Issue(code, severity, fld, message, span))

    if merchant_id != context.merchant_id:
        add("MERCHANT_SCOPE", "block", None, "draft is for a different merchant than its context")

    # independent scan of the raw text
    seen = set()
    for code, severity, pattern in SCANNER:
        m = pattern.search(text)
        if m and code not in seen:
            seen.add(code)
            add(code, severity, None, _SCANNER_MESSAGES[code], (m.start(), m.end()))
    for r in extraction.refused_requests:
        sev = (
            "clarify"
            if r.kind in ("instructions_inside_document", "override_or_remove_reserve")
            else "info"
        )
        add(
            f"MODEL_REFUSED_{r.kind.upper()}",
            sev,
            None,
            f"not applied ({r.kind.replace('_', ' ')})",
            locate(text, r.quote),
        )
    for a in extraction.ambiguities:
        add("AMBIGUOUS", "clarify", a.field, a.question, locate(text, a.quote))

    cur = context.current
    spans: dict[str, tuple[int, int, str]] = {}
    values: dict[str, int | None] = {}

    for fld in MONEY_FIELDS:
        mention: MoneyMention | None = getattr(extraction, fld)
        if mention is None:
            values[fld] = None
            continue
        span = locate(text, mention.quote)
        if span is None:
            add("UNGROUNDED", "block", fld, "the model quoted text that is not in your instruction")
            values[fld] = None
            continue
        quote = text[span[0] : span[1]]
        cur_code = mention.currency.strip().casefold()
        if _FOREIGN.search(quote) or (cur_code and cur_code not in _USD):
            add(
                "UNSUPPORTED_CURRENCY",
                "block",
                fld,
                "only USD amounts are supported (no currency conversion)",
                span,
            )
            values[fld] = None
            continue
        if not cur_code and "$" not in quote and not re.search(r"usd|dollar", quote, re.I):
            add("MISSING_CURRENCY", "clarify", fld, "state the currency (USD)", span)
        cents = _cents(mention.amount)
        if cents is None:
            add("INVALID_AMOUNT", "clarify", fld, "write the amount as digits, e.g. 500.00", span)
            values[fld] = None
            continue
        if cents not in _numbers_in(quote):
            add(
                "AMOUNT_NOT_IN_TEXT",
                "clarify",
                fld,
                "the amount must appear in your instruction as digits",
                span,
            )
            values[fld] = None
            continue
        if cents > MAX_CENTS[fld]:
            add("OUT_OF_BOUNDS", "block", fld, "amount is above the supported bound", span)
            values[fld] = None
            continue
        values[fld] = cents
        spans[fld] = (span[0], span[1], quote)

    # dates
    tz = ZoneInfo(context.business_tz)
    dates: dict[str, str | None] = {}
    for fld in ("policy_valid_until", "offer_response_deadline"):
        dm: DateMention | None = getattr(extraction, fld)
        if dm is None:
            dates[fld] = None
            continue
        span = locate(text, dm.quote)
        if span is None or locate(text, dm.phrase) is None:
            add("UNGROUNDED", "block", fld, "the model quoted text that is not in your instruction")
            dates[fld] = None
            continue
        d = parse_explicit_date(dm.phrase)
        if d is None:
            add(
                "AMBIGUOUS_DATE",
                "clarify",
                fld,
                f"'{dm.phrase}' is not an exact date; give a calendar date such as 2026-11-20",
                span,
            )
            dates[fld] = None
            continue
        end = datetime.combine(d, time(23, 59, 59), tzinfo=tz)
        if end <= as_of:
            add("PAST_DATE", "clarify", fld, "that date has already passed", span)
            dates[fld] = None
            continue
        dates[fld] = end.isoformat()
        spans[fld] = (span[0], span[1], text[span[0] : span[1]])

    # counterparties (own network only)
    def resolve(ms: list[CounterpartyMention], fld: str) -> tuple[str, ...]:
        out = []
        for c in ms:
            span = locate(text, c.quote)
            if span is None:
                add(
                    "UNGROUNDED",
                    "block",
                    fld,
                    "the model quoted text that is not in your instruction",
                )
                continue
            key = _norm(c.name).casefold()
            mid = context.counterparties.get(key)
            if mid is None:
                add(
                    "UNKNOWN_COUNTERPARTY",
                    "clarify",
                    fld,
                    f"'{c.name}' is not one of your counterparties",
                    span,
                )
                continue
            if mid == context.merchant_id:
                add("SELF_REFERENCE", "clarify", fld, "you cannot list yourself", span)
                continue
            out.append(mid)
        return tuple(sorted(set(out)))

    # every date-like phrase in the text must be covered by an extracted date field
    covered = [
        (s[0], s[1])
        for f, s in spans.items()
        if f in ("policy_valid_until", "offer_response_deadline")
    ]
    for dm in (extraction.policy_valid_until, extraction.offer_response_deadline):
        if dm is not None and (sp := locate(text, dm.quote)) is not None:
            covered.append(sp)
    for m_ in _DATE_LIKE.finditer(text):
        if not any(a <= m_.start() and m_.end() <= b for a, b in covered):
            add(
                "UNCAPTURED_DATE",
                "clarify",
                None,
                f"'{m_.group(0)}' looks like a date or deadline that the draft does not capture; "
                "give an exact calendar date or confirm it does not apply",
                (m_.start(), m_.end()),
            )
            break

    # participation / approval choices must be stated in the text, not inferred
    says_in = bool(_OPT_IN.search(text))
    says_out = bool(_OPT_OUT.search(text))
    if extraction.participation == "opt_in" and (not says_in or says_out):
        add("UNGROUNDED", "block", "participation", "the instruction does not say to opt in")
    if extraction.participation == "opt_out" and not says_out:
        add("UNGROUNDED", "block", "participation", "the instruction does not say to opt out")
    approval = extraction.approval_preference
    if approval == "approve_each_payment" and not _APPROVE_EACH.search(text):
        # never applied; dropping an unrequested extra approval step loses nothing
        add(
            "UNGROUNDED_PREFERENCE_IGNORED",
            "info",
            "approval_preference",
            "the model suggested approving each payment, but the instruction does not say so; "
            "ignored",
        )
        approval = "unspecified"

    allowed = resolve(extraction.allowed_counterparties, "allowed_counterparties")
    prohibited = resolve(extraction.prohibited_counterparties, "prohibited_counterparties")
    both = set(allowed) & set(prohibited)
    if both:
        add(
            "CONFLICT",
            "clarify",
            "counterparties",
            f"{', '.join(sorted(both))} is both allowed and prohibited",
        )

    if source == "document_text" and (
        any(v is not None for v in values.values())
        or any(dates.values())
        or allowed
        or prohibited
        or extraction.participation != "unspecified"
    ):
        add(
            "DOCUMENT_CANNOT_SET_POLICY",
            "block",
            None,
            "policies come only from your own instruction; text inside documents is data",
        )

    # merge with the confirmed policy and refuse relaxations
    def merged(new, old):
        return old if new is None else new

    proposed = ProposedPolicy(
        merchant_id=context.merchant_id,
        reserve_floor_cents=merged(values["reserve_floor"], cur.reserve_floor_cents),
        max_early_commitment_cents=merged(
            values["max_early_commitment"], cur.max_early_commitment_cents
        ),
        min_early_discount_cents=merged(values["min_early_discount"], cur.min_early_discount_cents),
        min_net_benefit_cents=merged(values["min_net_benefit"], cur.min_net_benefit_cents),
        fee_ceiling_cents=merged(values["fee_ceiling"], cur.fee_ceiling_cents),
        allowed=tuple(sorted(set(cur.allowed) | set(allowed))) if (allowed or cur.allowed) else (),
        prohibited=tuple(sorted(set(cur.prohibited) | set(prohibited))),
        valid_until=dates["policy_valid_until"],
        offer_response_deadline=dates["offer_response_deadline"],
        participation=(
            cur.participation
            if extraction.participation == "unspecified"
            else extraction.participation
        ),
        approval_preference=(cur.approval_preference if approval == "unspecified" else approval),
        spans=spans,
    )
    changes: list[dict[str, object]] = []

    def compare(fld, new, old, looser_if):  # looser_if(new, old) -> True when relaxed
        if new is None or new == old:
            return
        kind = "new" if old is None else ("relax" if looser_if(new, old) else "tighten")
        changes.append({"field": fld, "from": old, "to": new, "kind": kind})
        if kind == "relax":
            add(
                "RELAXATION_NOT_ALLOWED_VIA_AI",
                "block",
                fld,
                "this loosens a confirmed restriction; change it directly "
                "(not through an AI draft)",
                spans.get(fld, (None, None))[:2] if fld in spans else None,
            )

    compare("reserve_floor", values["reserve_floor"], cur.reserve_floor_cents, lambda n, o: n < o)
    compare(
        "max_early_commitment",
        values["max_early_commitment"],
        cur.max_early_commitment_cents,
        lambda n, o: n > o,
    )
    compare(
        "min_early_discount",
        values["min_early_discount"],
        cur.min_early_discount_cents,
        lambda n, o: n < o,
    )
    compare(
        "min_net_benefit", values["min_net_benefit"], cur.min_net_benefit_cents, lambda n, o: n < o
    )
    compare("fee_ceiling", values["fee_ceiling"], cur.fee_ceiling_cents, lambda n, o: n > o)
    if cur.allowed and set(allowed) - set(cur.allowed):
        add(
            "RELAXATION_NOT_ALLOWED_VIA_AI",
            "block",
            "allowed_counterparties",
            "widening the allowed counterparties loosens a confirmed restriction",
        )
    if set(cur.prohibited) & set(allowed):
        add(
            "RELAXATION_NOT_ALLOWED_VIA_AI",
            "block",
            "allowed_counterparties",
            "a confirmed prohibited counterparty cannot be re-allowed through an AI draft",
        )
    if cur.participation == "opt_out" and extraction.participation == "opt_in":
        add(
            "RELAXATION_NOT_ALLOWED_VIA_AI",
            "block",
            "participation",
            "opting back in must be done directly",
        )
    for mid in sorted(set(prohibited) - set(cur.prohibited)):
        changes.append(
            {"field": "prohibited_counterparties", "from": None, "to": mid, "kind": "tighten"}
        )

    # informational checks against verified data and existing commitments
    if (
        proposed.reserve_floor_cents is not None
        and context.evidence_reserve_cents is not None
        and proposed.reserve_floor_cents < context.evidence_reserve_cents
    ):
        add(
            "EVIDENCE_RESERVE_HIGHER",
            "info",
            "reserve_floor",
            "your verified capacity evidence protects more; the higher reserve applies",
        )
    if (
        proposed.max_early_commitment_cents is not None
        and context.committed_cents > proposed.max_early_commitment_cents
    ):
        add(
            "EXISTING_COMMITMENTS_PROTECTED",
            "info",
            "max_early_commitment",
            "cash already committed stays committed; the cap applies to new plans",
        )

    for fld in MONEY_FIELDS:
        if values[fld] is not None and not any(c["field"] == fld for c in changes):
            add(
                "ALREADY_IN_EFFECT",
                "info",
                fld,
                f"{fld.replace('_', ' ')} is already set to this value",
            )
    has_policy = (
        any(v is not None for v in values.values())
        or any(dates.values())
        or bool(allowed or prohibited)
        or extraction.participation != "unspecified"
        or approval != "unspecified"
    )
    if not has_policy and not any(i.severity == "block" for i in issues):
        add("NO_POLICY_FOUND", "clarify", None, "no policy change was found in the instruction")

    if any(i.severity == "block" for i in issues):
        status = Status.REJECTED
    elif any(i.severity == "clarify" for i in issues):
        status = Status.NEEDS_CLARIFICATION
    else:
        status = Status.READY_FOR_CONFIRMATION
    return ValidationReport(
        status=status,
        issues=tuple(issues),
        proposed=proposed if status is not Status.REJECTED else None,
        changes=tuple(changes),
        explanation=explain(status, proposed, issues, changes),
    )


def _usd(cents: int | None) -> str:
    return "not set" if cents is None else f"USD {cents // 100}.{cents % 100:02d}"


def explain(status: Status, p: ProposedPolicy, issues, changes) -> str:
    lines = []
    if status is Status.REJECTED:
        lines.append("This draft was refused; nothing was changed.")
    elif status is Status.NEEDS_CLARIFICATION:
        lines.append(
            "This draft needs your answers before it can be confirmed; nothing was changed."
        )
    else:
        lines.append("Draft ready for your review. It is NOT active until you confirm it.")
    labels = {
        "reserve_floor": "protected reserve",
        "max_early_commitment": "maximum early-payment commitment",
        "min_early_discount": "minimum early-payment discount",
        "min_net_benefit": "minimum net benefit",
        "fee_ceiling": "fee ceiling per payment",
    }
    for c in changes:
        f = c["field"]
        if f in labels:
            lines.append(f"- {labels[f]}: {_usd(c['from'])} -> {_usd(c['to'])} ({c['kind']})")
        else:
            lines.append(f"- {f}: add {c['to']}")
    if status is not Status.REJECTED:
        if p.valid_until:
            lines.append(f"- applies until {p.valid_until}")
        if p.offer_response_deadline:
            lines.append(f"- offers must be decided by {p.offer_response_deadline}")
    for i in issues:
        if i.severity != "info" or status is not Status.READY_FOR_CONFIRMATION:
            lines.append(f"* {i.message}")
        else:
            lines.append(f"(note) {i.message}")
    return "\n".join(lines)
