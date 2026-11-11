# ADR-0010: Phase 3 AI policy interpretation and bounded negotiation

- **Date:** 2026-11-11 · **Status:** Proposed (pending owner review of P3; no frozen decision changed)
- **Context:** Phase 3 adds the bounded LLM layer (execution plan Phase 3; docs 06, 07, 09,
  11, 12, 13, 16; D003, D004, D005, D006). The owner authorized up to **USD 10** of model
  inference on an existing **Nebius Token Factory** balance (no credit purchases, no
  automatic top-ups, no dedicated endpoints) and asked for a cost-effective serverless
  model chosen after checking the live model list and structured-output support.
- **Affected files:** `api/ai/*`, `api/policy/*`, `api/negotiation/*`, `api/app/demo_ai.py`,
  `api/app/main.py`, `api/config.py`, `.env.example`, `.gitignore`, `fixtures/ai_demo.py`,
  `fixtures/ai_eval_cases.py`, `scripts/llm_probe.py`, `scripts/run_ai_eval.py`,
  `tests/unit/ai/*`, `tests/contract/test_demo_ai_api.py`, `docs/api/P3_DEMO_API.md`.

## Decisions

1. **Separation of duties.** `api/ai` only produces schema-validated JSON (no tools, no
   function calling, no payment or provider code; a P0 test forbids those imports).
   Whether a rule is safe is decided by deterministic code in `api/policy` and
   `api/negotiation`, which never calls a model.
2. **Provider.** OpenAI-compatible chat completions over `httpx` (already a dependency; no
   new SDK). `LLM_PROVIDER=nebius` defaults to `https://api.tokenfactory.nebius.com/v1/`.
   Requests use `response_format: {"type": "json_schema", "json_schema": {name, schema,
   strict: true}}`, temperature 0 and never a `tools` field. HTTPS only. The key is a
   `SecretStr` from the untracked `.env`, used only in the Authorization header.
3. **Model choice.** Made from the live `/v1/models` list plus a strict-JSON probe
   (`scripts/llm_probe.py`), recorded in the tracker with the probe evidence. Default:
   `Qwen/Qwen3-235B-A22B-Instruct-2507`, with `openai/gpt-oss-120b` as the alternative.
   Both are about USD 0.15–0.20 per million input tokens and 0.60 per million output
   tokens, as mirrored from Nebius's public list on 2026-11-11; Nebius calls these prices
   approximate.
4. **Spend cap.** Every real call is checked against `LLM_SPEND_CAP_USD` (default 10.00)
   *before* it is sent. The check uses an upper-bound estimate: inflated input tokens plus
   the full `max_tokens`, priced at 1.5× the listed rates. The cost is then recorded from
   the reported usage in `.elide/llm_spend.json`, which is git-ignored. A model without a
   known or configured price cannot be called. These are estimates; the provider console
   is the bill.
5. **Typed output, untrusted.** `PolicyExtraction`, `CounterofferProposal` and
   `PlanExplanation` keep amounts as strings exactly as written, with no floats. Every value
   carries a verbatim quote. Relative dates are copied, never resolved. "Not stated" is
   null, never a guessed zero. Results carry a `ModelCall`: provider, model, prompt
   version, `mocked` flag, tokens, estimated cost and a SHA-256 of the exact context.
   Mocked output is always labelled `MOCK_MODEL_OUTPUT`.
6. **Deterministic policy validation.**
   - **Grounding:** each quote must be found in the original text, which yields the source span. An amount must literally appear in its quote, so a changed or invented number is refused. Participation and approval choices need matching words in the text.
   - **Money:** exact USD only; CAD, EUR and the like are refused (no conversion, D008); values must be within bounds.
   - **Dates:** only explicit calendar dates, resolved in the merchant's time zone. Relative, partial or past dates go to clarification.
   - **Scope:** counterparties must come from the merchant's own network. The draft must belong to the context's merchant.
   - **No relaxation via AI:** a draft may add or tighten restrictions but never loosen a confirmed one. Loosening needs a direct, authenticated merchant edit (P4).
   - **Documents:** pasted documents cannot set policy.
   - **Scanner:** an independent regex scan of the raw text flags override or injection attempts, payment-authorization requests, impersonation, disclosure requests and balance claims, whatever the model reported. Balances come only from capacity evidence.
   - **Outcome:** `REJECTED`, `NEEDS_CLARIFICATION` or `READY_FOR_CONFIRMATION`, with a deterministic explanation and a digest.
7. **Activation.** `PolicyStore.confirm` requires the same merchant, a READY draft and
   the exact digest; only a `ConfirmedPolicy` can change planning inputs
   (`apply_confirmed_policy`, tighter only: reserve floor, max early commitment as a
   reserve raise, min discount/benefit, fee ceiling, prohibited/allowed counterparties,
   opt-out, expiry). Confirmation is a Python interface, **not an HTTP endpoint**, until
   P4 provides authenticated sessions.
8. **Bounded counteroffer.** The P2 solver diagnoses why the invoice is not payable. The
   reasons are requester-visible codes only. Deterministic bounds follow:
   - lowest: the requester's minimum discount, and the smallest concession that fits its deployable cash;
   - highest: the requester's own cap, and the counterparty's *explicitly disclosed* maximum, if any;
   - no range means no model call.

   The model receives only the invoice, its face and open amount, the counterparty's current offer, the allowed range and the reason codes. It never receives the requester's reserve, balance or hurdle rate, or the counterparty's private rules.

   Deterministic checks on the result:
   - the amount is exact and within the range;
   - the message contains no private figures, no invented amounts, no claim that anyone has accepted or approved, and no claim that funds are available or guaranteed.

   The result is an **unsigned DRAFT revision** that counteroffers the open offer. It goes onto a copy of the invoice record; the in-force terms are asserted unchanged. P2 then re-plans with the draft as a proposal assumed to be signed by both parties (a simulation).

   Views:
   - the requester sees its own modeled economics and "counterparty approval required";
   - the counterparty's declared rules are evaluated separately and shown only in its own view.

   No approval evidence is ever created.
9. **Explanations.**
   - The plan record is re-verified first (`verify_plan_record`). A stale or altered plan is refused before any model call.
   - Facts come only from the viewer's `merchant_view`, plus infeasibility reasons about the viewer itself.
   - Model text may use only amounts present in the facts and name no merchant outside the view; otherwise the deterministic template is used.
10. **API.** Four synthetic-only demo endpoints under `/v1/demo/`: policy draft, policy
    validate, counteroffer, plan explain. They accept fixture merchants and scenarios only
    and forbid unknown fields. Responses separate `model_output` (untrusted, REAL or MOCK)
    from `deterministic` results, and write nothing to the database. There are no
    confirmation, approval, signing or payment endpoints.
11. **Evaluation.** `policy-eval-v1` has 40 labelled synthetic cases across 15 categories
    (ordinary, conflict, ambiguous dates, unsupported currency, missing thresholds, reserve
    overrides, malicious invoice text, prompt injection, impersonation, disclosure,
    invented balances, unaffordable discounts, bounds, payment authorization, unknown
    counterparty). It also has 12 "compromised model" outputs, which the validator must
    never pass as READY. `scripts/run_ai_eval.py` reports, separately for MOCK reference
    extractions and REAL model runs:
    - schema validity
    - status agreement
    - exact extraction of labelled values
    - abstentions and false abstentions
    - **unsafe passes (must be 0)**

## Alternatives considered

- **Anthropic API / an official SDK:** not chosen; the owner selected Nebius.
- **The `openai` SDK pointed at Nebius:** unnecessary for one endpoint; `httpx` keeps the
  dependency set and request shape fully auditable.
- **Agent frameworks, tool use, vector stores:** out of scope (doc 07; owner instruction).
- **Letting the model resolve dates or currencies:** rejected; those are deterministic.
- **Allowing relaxations through the AI path with extra confirmation:** deferred to P4's
  authenticated direct edit; the AI path is tighten-only.

## Finance / privacy impact

No payment, invoice, reserve or commitment can be changed by model output. Private
figures are not sent to the model in negotiation, and outbound texts are checked for
leaks. Policy text is sent to the model provider (synthetic demo data only in P3). P4
must add tenant scoping and redaction before any real merchant text is used.

## Limitations

- Regex-based scanning is a safety net, not a classifier. A novel phrasing can evade it.
  The non-relaxation and grounding rules still hold whatever the model says.
- Dates need explicit calendar dates; this is safe but less convenient.
- One counteroffer type (an early-payment discount). Timing changes and instalments are not implemented.
- Confirmation happens in process, without authentication (P4).
- There is no persistence of drafts or confirmed policies yet.
- The model-quality metrics come from 40 synthetic cases and are not field validation.
