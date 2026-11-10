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
