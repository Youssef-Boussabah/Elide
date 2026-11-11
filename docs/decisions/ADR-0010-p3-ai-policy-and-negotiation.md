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

## Addendum (2026-11-11): real inference, model choice, opportunity analysis

12. **Model choice (from real calls).** The live `/v1/models` list returned 23 models.
    Strict-JSON probes passed on `Qwen/Qwen3-235B-A22B-Instruct-2507` and
    `Qwen/Qwen3-30B-A3B-Instruct-2507`. `openai/gpt-oss-120b` spent its 200-token probe on
    hidden reasoning and returned no content. All three were evaluated on
    `policy-eval-v1`; **Qwen3-235B-A22B-Instruct-2507** was selected and stays the default.
    It had the best status agreement and exact values, 0 schema failures in 160 calls, and
    the lowest false-abstention rate.
13. **Changes made because of real-model results.** The eval set is a *development* set,
    not a held-out test.
    - Prompt `policy-extract-v2`: Qwen3-235B on v1 added an unrequested
      `approve_each_payment` to 9 of 40 outputs. The deterministic grounding check caught
      every one, but they turned safe drafts into REJECTED. v2 makes `unspecified` the
      explicit default.
    - The validator now ignores an ungrounded approval preference, with a note, instead of
      blocking the draft. The preference is never applied, so nothing is lost.
    - New `UNCAPTURED_DATE` check: every date-like phrase in the text must lie inside a
      captured date field. gpt-oss-120b dropped "through Friday" from "Keep $600 for
      payroll through Friday", and the old validator let that draft through as READY, so
      the eval counted it as an unsafe pass. The reserve would have lasted longer than
      intended (stricter, not looser), but the misreading must still go to clarification.
      The real output is kept as adversarial case `adv-13`.
14. **Spend control in practice.** All calls were sequential. The local estimate (1.5×
    listed prices, upper bound) totalled USD 0.123. The Nebius console showed trial
    credits falling from USD 0.99 to USD 0.93, an actual spend of about USD 0.06; the
    USD 50.00 account balance was untouched. The usage table lags (last updated 19:46
    UTC). The local cap is not a provider-side cap; the console is the source of truth.
15. **Proactive opportunity analysis** (`api/opportunities`, `elide.opportunities.v1`).
    Starting from the P2 network and evidence, without any prompt:
    - runs the verified plan and B0–B5;
    - lists bottlenecks with deterministic reasons, taken from the best *current* terms
      (proposals excluded): capacity, the payer's or payee's own rules,
      `COMPETES_WITH_PLAN`, `FUNDS_UNAVAILABLE`, currency, dispute, expiry;
    - recommends `RECOMMEND_PLAN`, `NEGOTIATE`, `NO_ACTION` or `BLOCKED`. `NEGOTIATE`
      needs a bounded discount hint, i.e. the best offer fails the payer's own minimum or
      cash, or a pending proposal would help;
    - compares exactly against B0, B1 and B3, and states when earliest-due-first already
      matches the plan;
    - flags participant modeled losses, uncertain or example fees, unavailable or blocked
      funds, the buyer earning below its own best plan, outstanding synthetic approvals
      and simulated downstream legs.

    `merchant_view` gives each merchant its own actions, bottlenecks, hints and benefit;
    others appear as counts, and the network comparison goes to the initiating merchant
    only. `facts` feeds the same checked narration used for plans. The analysis is
    read-only and deterministic.
16. **Paid-token routes are local-only.** Every route that can call the model answers
    loopback clients only (HTTP 403 otherwise) until P4 authentication exists. The
    deterministic routes `/policies/validate` and `/opportunities` stay open, but they use
    synthetic fixtures only and cost nothing.

## Addendum (2026-11-11): final-review hardening

17. **Recommendation semantics.** The analyzer now separates six things and keeps them
    apart in a `decision` block:
    - feasibility: a verified plan;
    - advantage over B0 (do nothing), B1 (earliest-due-first) and B3 (random tie-break),
      with `network_awareness_needed = B4 > B1`;
    - the buyer's net benefit;
    - each participant's net benefit;
    - fee and availability uncertainty;
    - outstanding consent: approvals required versus recorded.

    `RECOMMEND_PLAN` requires a feasible plan that pays more on time than B0, with no
    participant holding a modeled loss it has not declared it accepts. A merchant declares
    acceptance through `min_receipt_net_benefit_cents`, and the loss must lie within that
    declared threshold. When a plan improves on B0 but some participant has an undeclared
    loss, the result is **`REVIEW_ECONOMICS`**. When B1 already matches B4, the reason says
    it is not a network-aware improvement.

    Every recommendation is conditional. No approval is recorded, no downstream funds are
    verified spendable, and `execution.may_execute` stays false.

    | Case | Result |
    |---|---|
    | Canonical | `REVIEW_ECONOMICS`: B4 = B1 = $2,700 on time, buyer +$3.70, supplier −$10.00 |
    | Positive-sum, timing (constructed) | `RECOMMEND_PLAN` |
18. **Merchant-scoped views.** `merchant_view` derives its own recommendation and facts
    for each merchant; nothing global is copied into them.
    - **Every merchant:** its own legs, its own invoices, benefit, approvals and flags.
    - **The payer of an invoice:** also sees why the payment is blocked. The creditor sees
      only `PAYER_SIDE`.
    - **Merchants with no leg:** receive `NOTHING_FOR_YOU`.
    - **Network-level data** (totals, counts, flags such as the number of approvals, and
      baseline comparisons) goes to the initiating merchant only. Even that merchant
      receives only its own net benefit and a *count* of participants with undeclared
      losses, because other participants' nets reflect their private inputs.
    - **Model prompts** are built only from these views.
    - **The P2 plan explanation** also stops telling non-initiating merchants how many
      other payments exist.

    Adversarial tests on `privacy-probe-v1` cover views and model prompts. It uses
    distinctive private cash, reserves, a hidden counterparty and invoice ids, and private
    supplier rules in the counteroffer prompt. P3 remains synthetic: in P4 the viewer must
    be the authenticated tenant principal.
19. **Atomic spend reservation.** `SpendGuard.reserve` adds the upper-bound estimate inside
    one critical section and refuses if committed + outstanding + estimate would exceed
    the cap. The section is held under a process-wide thread lock and an exclusive lock
    file next to the ledger, so threads and separate processes cannot over-reserve. After
    the call:
    - success: `commit` replaces the reservation with the usage-based estimate;
    - certainly not sent (connection error, non-200): `release`;
    - unknown outcome (timeout): commit at the full estimate.

    A held lock fails closed and is never broken automatically. Tests cover 16 threads and
    8 processes. This remains a **local software cap over estimates**, not a
    provider-side billing limit.
20. **Evaluation honesty.** The 40-case `policy-eval-v1` set was used for development and
    tuning: prompt v2, the approval-preference change and the `UNCAPTURED_DATE` check.
    It is not an independent held-out evaluation.

    | Run | Result |
    |---|---|
    | Selected Qwen3-235B, final run | 38/40 status agreement, 16/17 exact values, 0 schema failures, 1 provider-call failure (`MODEL_UNAVAILABLE`), 0 unsafe passes |
    | Earlier v2 runs | 39/40 |
    | gpt-oss-120b, before the date check | the one unsafe pass, preserved as evidence and as `adv-13` |

    Spend: about USD 0.06 actually consumed, from the Nebius console (trial credits
    0.99 → 0.93), against a local upper-bound estimate of USD 0.123.
