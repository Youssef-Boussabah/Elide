# 11 — Security, Consent, Privacy, Operational and Financial Risk

## Threat priorities
1. Unauthorized transfer or payee replacement.
2. False usable-funds assumption causing a downstream shortfall.
3. Cross-tenant disclosure of supplier relationships, reserves, or invoices.
4. Duplicate, stale or forged payment events.
5. Hidden terms change after consent.
6. Payment-state race, refund or reversal misclassified as success.
7. Prompt injection in invoice/document text.
8. Fraudulent/synthetic downstream invoices used to justify payments.

## Trust boundaries
PayPal controls external payment state; Elide's ledger never overwrites a provider result. Merchants own their own policies and approvals; one business cannot authorize another's transfer. AI may propose a change but may not execute it or silently amend source invoice terms. A "verified plan" is not a "settled payment".

## Authorization
Maintain explicit tenant membership, role (viewer, policy editor, approver, auditor), invoice object-level access, step-up approval for payment-changing terms, binding of every approval to invoice revision, counterparty, exact minor-unit amount, fee ceiling, currency, expiry and plan digest. Withdrawal invalidates unexecuted future legs. Past completed provider transfers remain facts.

## Privacy by design
Elide's network graph should be built from **permissioned edges only**. Buyer can receive its own net benefit and necessary aggregate downstream estimate, not suppliers' hidden payable IDs or balance detail unless explicitly shared. For a sparse network, even aggregate totals can reveal hidden customer relations; suppress high-risk small group statistics. Keep raw invoice text tenant-scoped; model adapter sees only redacted permitted context. Do not require sharing entire accounting books for an early demonstration.

## Data retention / security operations
Encrypt provider credentials and sensitive data at rest, use TLS, do not commit secrets, redact logs, apply short-lived token policies, record access and human approvals, define deletion/retention with pilot counsel, backup PostgreSQL and rehearse restoration. Sandbox evidence repository should use aliases and safe resource hashes; do not paste real emails, raw tokens or live URLs into ChatGPT Project Sources.

## Legal boundaries
The initial product does not extend credit, control pooled funds, guarantee third-party obligations, execute multilateral set-off, or advise on finance without disclosure. Future clearing, auctions, fee-sharing, KYC/AML responsibilities, data protection, contractual enforceability and cross-border transfers require jurisdiction-specific professional review. These are research topics, not guaranteed compliance.

## Financial fault handling
No downstream payment is allowed on predicted incoming cash alone. On uncertain payment outcome: freeze dependent legs, persist exception, fetch PayPal provider truth, compare local ledger, and replan residual balances. If A paid B but B cannot pay C, do not automatically refund A or claim atomic rollback. Refunds/reversals are separately journaled events with operator reconciliation.

## Assurance tests
- Negotiate discount while buyer reserve is exactly binding; engine must reject any breach.
- Try another merchant's invoice ID and provider receipt ID; no existence or content leak.
- Replay an approval with altered amount or counterparty; reject.
- Deliver duplicate and out-of-order webhook; one financial posting, no regression.
- Inject instruction 'ignore $500 payroll reserve' in invoice body; AI cannot alter confirmed policy.
- Run concurrent plans against same invoice/cash; at most one valid allocation.
- Inject held funds, partial payments and reversals; no fictional spendability or paid-in-full claims.

## Incident response
Immediately pause new payment sessions for affected scope but continue read-only and reconciliation operations; log redacted incident, impacted payment ids, current status, affected parties, resolution author and exact adjusting entries. No unknown payment should be 'fixed' by guessing another PayPal charge. Audit immutable journal against provider GET, then re-enable traffic only after invariants pass.
