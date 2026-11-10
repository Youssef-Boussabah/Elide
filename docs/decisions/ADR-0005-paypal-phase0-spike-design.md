# ADR-0005: PayPal Phase 0 spike design

- **Date:** 2026-11-10 · **Status:** Accepted
- **Context:** P0 must prove merchant-owned Invoicing v2 access and a hosted payer route
  without assuming partner scopes (10_PAYPAL_INTEGRATION_RUNBOOK, 03_PAYPAL_STRATEGY).
- **Decision:** `scripts/paypal_sandbox_check.py` runs five opt-in steps: OAuth token,
  `GET /v2/invoicing/invoices`, draft creation (`POST /v2/invoicing/invoices` with
  `PayPal-Request-Id`), send (`POST .../send`, looking for a `payer-view` link), and
  `GET` of the invoice. The draft mirrors the canonical fixture: $1,000.00 face with an
  invoice-level $10.00 discount. The script reads PayPal's computed total back and
  compares it to 99,000 cents instead of assuming discount support.
- **Safety:** Sandbox base URL is hard-coded and enforced. Secrets are `SecretStr`. Saved
  evidence is redacted (emails, names, payer URLs, tokens). Payment is a human action in
  the PayPal-hosted page. The script never pays, captures, refunds or marks paid.
- **Fallback (not yet built):** If Invoicing cannot support the flow, use Orders v2 hosted
  approval/capture for an Elide-owned invoice, labeled as not a PayPal invoice.

## Sandbox observations (2026-11-10, Specified → Sandbox-verified for steps 1–3 and 5)

- **Discount sign convention:** we send `amount.breakdown.discount.invoice_discount.amount`
  as `"10.00"`. PayPal stores and returns it as `"-10.00"`, with `amount.value` and
  `due_amount` already reduced to `"990.00"`. `api/integrations/paypal/invoice_terms.py`
  accepts a leading `-` on that one field only (strict everywhere else) and records the
  sign. Evidence: `evidence/paypal-sandbox/runs/20261110T214515Z.json`.
- **`generate-next-invoice-number` needs `Content-Type: application/json`:** without it
  the body-less POST returned HTTP 415 `UNSUPPORTED_MEDIA_TYPE` (debug id
  `f81253945e66d`). With the header: HTTP 200. When no number is sent, PayPal assigns
  one itself (`0001`). Evidence: runs `20261110T214515Z` (415) and `20261110T214721Z` (200).
- **Defaults PayPal applied to the draft:** `payment_term.term_type = NO_DUE_DATE`,
  `tax_total = 0.00`, `invoice_date` = creation date. Elide does not yet set a due date;
  P1/P5 must decide whether invoices carry an explicit `due_date`.
