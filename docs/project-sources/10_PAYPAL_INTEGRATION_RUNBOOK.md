# 10 — PayPal Sandbox Integration Runbook

## Objective and proof boundary
Create, pay and reconcile **one real PayPal Sandbox payment** on a merchant-owned invoice. Everything beyond confirmed provider facts must be labeled as simulated, merchant-attested, or projected. An external PayPal invoice may be paid outside Elide; reconciliation must fetch provider truth before creating an extra payment intent.

## Setup
1. Create PayPal Sandbox business and personal accounts; configure REST application credentials in an encrypted local secret store / `.env` excluded by Git.
2. Confirm API accessibility with OAuth token request and a harmless GET. Record account/app environment and endpoint support.
3. Find a supported invoice workflow. Test whether the account can create an invoice, send or host it, and set/modify terms as intended. Confirm exact API support and commercial permission boundary.
4. For partner operations, follow **select-partner-only** onboarding and merchant permission flow, rather than assuming all business profiles are readable. See https://developer.paypal.com/platforms/invoicing/integrate
5. Record redacted request/response fixtures and source URLs, not secrets or live payer links in a public repository.

## Payment walkthrough
- Create owned invoice with stable local ID, merchant ownership and correct currency.
- Present a quote with approved concession and expiry; if native discount fields are unsupported, use an explicit creditor-authorized invoice revision process or other supported provider path. Do **not** change economic terms in Elide alone and then pretend PayPal invoice matches.
- Buyer reviews exact payer, amount, merchant, currency, invoice identity and complete terms before using PayPal-hosted action.
- Obtain actual Sandbox payer completion; fetch authoritative invoice/payment resource. Verify amount and currency, including outstanding part, fees if reported, and provider identifiers.
- Persist a single immutable posting even if multiple webhooks arrive.
- Make next leg merely **eligible** for reconsideration if and only if the payer's current spendable cash is separately verified/attested and its own authorization remains valid.

## Status and evidence distinctions
`provider_pending`: request accepted or payment initiated, not yet fully reconciled.
`provider_confirmed`: provider resource independently fetched and amount/status recognized; check partials/refunds.
`funds_usable`: independently supported by relevant merchant capacity evidence within validity window. **Not inferred from a webhook or balance report that may lag.**
`fully_reconciled`: provider amounts and local invoice/journal match, exceptions resolved.

## Webhooks and retries
- Validate webhooks using PayPal's documented verification mechanism with original transmission metadata and configured webhook ID.
- Put raw event identity and payload hash in durable inbox; deduplicate by provider event ID + environment, then fetch authoritative state.
- Never post the same money movement twice, and never let late `pending` regress a reconciled state.
- Honor documented endpoint-specific `PayPal-Request-Id` semantics. An unknown result after timeout requires lookup/manual reconciliation; avoid blind retries that charge twice.
- Ensure operation records persist before calls and no SQL lock is held through API latency.

## Failure decision table
| Condition | Behavior |
|---|---|
| 401/token expired | Refresh safely within supported retry policy; never log token |
| 403/insufficient partner permission | Record integration limitation and stop that path |
| 409 terms changed | Invalidate approval, refresh plan and reconfirm |
| payment held or unverified available | Halt downstream actions; show blocked reason |
| webhook delivered twice | One inbox economic event, one journal posting |
| webhook arrives after owner revokes | Reconcile external fact; do not schedule new downstream transfer |
| provider timeout with unknown create result | Fetch known identifiers, otherwise manual exception; no blind repeat |
| partial payment | Record partial settlement, residual invoice, replan if mode supports it |
| reversal after full settlement | Compensating postings, operator review, don't rewrite historical receipt |

## Required evidence manifest
`commit_sha`, `run_id`, `fixture_hash`, `environment`, redacted merchant IDs, operation keys, invoice provider reference, intended amount, observed provider amount/status/time, fees/concession ledger, availability evidence type, approval digest, exception list, replay logs. Keep access tokens, test account credentials, real emails, and private payment URLs out of public materials.

## Source links
- https://developer.paypal.com/docs/invoicing/
- https://developer.paypal.com/api/invoicing/v2/invoices-update/
- https://developer.paypal.com/api/rest/webhooks/rest/
- https://developer.paypal.com/api/rest/reference/idempotency/
- https://developer.paypal.com/platforms/invoicing/integrate

## Unverified until implementation
Exact discount behavior of chosen merchant account; timing and content of fee availability; partner-specific permissions; usable balance evidence; number of PayPal Sandbox accounts available; any payment behavior unsupported by actual receipts. Do not hardcode optimistic answers in the user interface.
