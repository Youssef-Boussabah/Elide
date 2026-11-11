"""Deterministic checks on model-written text (messages, explanations).

- No private figures: amounts or rates private to a merchant must not appear anywhere.
- No invented amounts: every money amount in the text must be in the allowed set.
- No claims that anyone accepted, approved, agreed or signed, and no claims that funds are
  available, cleared or guaranteed: those are facts only real approvals and provider
  evidence can establish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MONEY = re.compile(
    r"(?:(?:US)?\$|USD\s?)\s?((?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.(\d{1,2}))?"
    r"|((?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.(\d{1,2}))?\s?(?:USD|dollars?)\b",
    re.I,
)
_ANY_NUMBER = re.compile(r"(?<![\w.])((?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.(\d{1,2}))?(?![\w,])")
ACCEPTANCE_CLAIM = re.compile(
    r"\b(has|have|had|already|is|are|was|were|we've|you've|they've)\s+(been\s+)?"
    r"(accepted|approved|agreed|signed|authori[sz]ed|confirmed)\b"
    r"|\b(accepted|approved|agreed to|signed off)\s+(by|on behalf of)\b"
    r"|\bdeal is (done|agreed|final)\b",
    re.I,
)
AVAILABILITY_CLAIM = re.compile(
    r"\bguarantee[ds]?\b|\b(funds?|money|cash|payment)\s+(is|are|will be)\s+(already\s+)?"
    r"(available|cleared|settled|guaranteed|usable)\b|\binstant(ly)?\s+(available|access)\b"
    r"|\balready (been )?paid\b",
    re.I,
)


def _cents(whole: str, frac: str | None) -> int:
    return int(whole.replace(",", "")) * 100 + int((frac or "").ljust(2, "0") or 0)


def money_amounts(text: str) -> set[int]:
    out = set()
    for m in _MONEY.finditer(text):
        if m.group(1) is not None:
            out.add(_cents(m.group(1), m.group(2)))
        elif m.group(3) is not None:
            out.add(_cents(m.group(3), m.group(4)))
    return out


def all_numbers(text: str) -> set[int]:
    return {_cents(m.group(1), m.group(2)) for m in _ANY_NUMBER.finditer(text)}


@dataclass(frozen=True, slots=True)
class TextIssue:
    code: str
    detail: str


def check_text(
    text: str,
    *,
    allowed_amounts: set[int],
    private_amounts: set[int] = frozenset(),
    private_strings: tuple[str, ...] = (),
    forbidden_names: tuple[str, ...] = (),
    max_chars: int = 1200,
) -> list[TextIssue]:
    issues: list[TextIssue] = []
    if len(text) > max_chars:
        issues.append(TextIssue("TOO_LONG", f"{len(text)} characters"))
    leaked = (all_numbers(text) & set(private_amounts)) - set(allowed_amounts)
    if leaked:
        issues.append(TextIssue("PRIVATE_AMOUNT", f"{len(leaked)} private figure(s) in the text"))
    low = text.casefold()
    for s in private_strings:
        if s and s.casefold() in low:
            issues.append(TextIssue("PRIVATE_DETAIL", "a private policy detail is in the text"))
            break
    for name in forbidden_names:
        if name and re.search(rf"\b{re.escape(name.casefold())}\b", low):
            issues.append(TextIssue("UNRELATED_MERCHANT", "names a merchant outside this view"))
            break
    invented = money_amounts(text) - set(allowed_amounts)
    if invented:
        issues.append(TextIssue("INVENTED_AMOUNT", f"{len(invented)} amount(s) not in the facts"))
    if ACCEPTANCE_CLAIM.search(text):
        issues.append(
            TextIssue("ACCEPTANCE_CLAIM", "claims an acceptance/approval that has not happened")
        )
    if AVAILABILITY_CLAIM.search(text):
        issues.append(TextIssue("AVAILABILITY_CLAIM", "claims funds are available/guaranteed"))
    return issues
