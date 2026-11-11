# P3 demo API (synthetic data only)

Base: `uv run uvicorn api.app.main:app --port 8000`. All endpoints are `POST`, accept JSON
with **no unknown fields** (`422` otherwise), operate only on the synthetic fixtures in
`fixtures/ai_demo.py` and `fixtures/planning.py`, write nothing to the database and never
confirm, sign, approve, reserve or pay anything. Every response has
`labels: ["SYNTHETIC_DEMO", "NOT_EXECUTABLE", "NO_APPROVALS_CREATED"]` and keeps
`model_output` (untrusted; `model_call.label` is `REAL_MODEL_OUTPUT` or
`MOCK_MODEL_OUTPUT`) separate from `deterministic` results.

Without `LLM_PROVIDER` + `LLM_API_KEY` in `.env`, model-backed endpoints return a
`MODEL_UNAVAILABLE` status (manual entry via `/policies/validate` still works).

## `POST /v1/demo/policies/draft`

Request:
```json
{"merchant_id": "buyer", "text": "Keep at least $600 protected for payroll.",
 "source": "merchant_message"}
```
`merchant_id`: `buyer | food_supplier | packaging_supplier | carrier | mechanic`;
`text`: 1–2000 characters; `source`: `merchant_message` (default) or `document_text`
(document text can never set policy).

Response (abridged):
```json
{"draft_id": "pdr_…", "status": "READY_FOR_CONFIRMATION", "outcome": "VALIDATED",
 "model_output": {"label": "UNTRUSTED_MODEL_OUTPUT", "extraction": {"reserve_floor":
   {"amount": "600", "currency": "$", "quote": "Keep at least $600 protected"}, "…": "…"}},
 "model_call": {"provider": "nebius", "model": "…", "prompt_version": "policy-extract-v1",
   "mocked": false, "label": "REAL_MODEL_OUTPUT", "input_tokens": 0, "output_tokens": 0,
   "estimated_cost_usd": "…", "context_sha256": "…"},
 "deterministic": {"label": "DETERMINISTIC_VALIDATION", "status": "READY_FOR_CONFIRMATION",
   "issues": [{"code": "…", "severity": "block|clarify|info", "field": "…",
     "message": "…", "span": [0, 28]}],
   "proposed": {"reserve_floor_cents": 60000, "spans": {"reserve_floor": [0, 28, "…"]}, "…": "…"},
   "changes": [{"field": "reserve_floor", "from": 50000, "to": 60000, "kind": "tighten"}],
   "explanation": "Draft ready for your review. It is NOT active until you confirm it. …",
   "digest": "…64 hex…", "activation": "not active; requires the merchant's explicit
   confirmation of this digest (authenticated confirmation arrives in P4)"}}
```
`status` is one of `READY_FOR_CONFIRMATION`, `NEEDS_CLARIFICATION`, `REJECTED`,
`MODEL_UNAVAILABLE`, `MODEL_OUTPUT_INVALID`.

## `POST /v1/demo/policies/validate`

Same request fields plus `extraction` (a `PolicyExtraction`, e.g. edited by the merchant
in the UI). Runs only the deterministic validator; `model_call` is `null`.

## `POST /v1/demo/negotiation/counteroffer`

Request: `{"scenario": "food_5_to_10", "viewer": "buyer" | "food_supplier",
"requester_max_concession": "15.00", "disclosed_max_concession": null | "12.00"}`.

Response: `status` (`DRAFT_PROPOSED`, `DRAFT_NOT_FEASIBLE`, `NO_BOUNDED_PROPOSAL`,
`NO_CHANGE_NEEDED`, `MODEL_UNAVAILABLE`, `MODEL_OUTPUT_INVALID`, `MODEL_OUTPUT_REJECTED`).

`model_output` contains the buyer's proposal, and is `null` for the supplier view.

`deterministic` contains:
- `reason_codes` and `allowed_range_cents` (buyer view only)
- `message_issues`
- `draft_revision`, as below
- `view`: the buyer's modeled economics, or the supplier's own rule check

The `draft_revision` has this shape: `{status: "draft", authorized_by: [], approval_evidence: null, concession_cents, payment_cents, supersedes, expires_at, required_approvals: [...]}`.

`message_for_counterparty` is set only when `status` is `DRAFT_PROPOSED`.

## `POST /v1/demo/plans/explain`

Request: `{"scenario": "two_candidates" | "reserve_shortfall", "viewer": "<merchant>",
"plan": null | <elide.plan.v1 record>}`. A supplied record is re-verified first; a stale or
altered one returns `STALE_OR_TAMPERED` with `plan_problems` and no model call.

Response:
- `status`: `EXPLAINED` or `TEMPLATE`
- `text` and `text_source` (`model` or `deterministic_template`)
- `deterministic.facts` (`F1`…), `deterministic.issues`, and `model_call`

## P4 integration contract

- Replace `merchant_id` in the request body with the authenticated principal; scope fixture lookups to tenant membership.
- Add an authenticated `POST /v1/policies/{draft_id}/confirm` with body `{digest}`. It calls `PolicyStore.confirm`; persist drafts and confirmed versions.
- Add authenticated signing of a negotiated `draft_revision` by **each** party separately. Only after both have signed may an AUTHORIZED revision be appended, through `InvoiceRecord.add_revision`, which checks both signatures.
- Use `merchant_view` and the counteroffer `requester_view` / `counterparty_view` as the only data sent to each merchant's UI.
