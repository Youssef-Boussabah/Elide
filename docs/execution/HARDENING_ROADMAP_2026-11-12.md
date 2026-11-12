# Hardening roadmap (2026-11-12)

Kept separate from the ADR-0013 work. **Nothing below is implemented** unless a row says so;
this file must not be read as a capability claim. Each item names its evidence gate and any
owner decision it needs.

**What P5 established (Sandbox-verified, one controlled pilot payment):**
- one invoice, created and delivered;
- a human payment on PayPal's hosted page;
- provider GET confirmation;
- independent reconciliation;
- idempotent replay for the tested scenarios.

| # | Item | Current state (fact) | Needed before claiming it | Owner decision / authorization |
|---|---|---|---|---|
| H1 | **Live webhook verification and delivery** (ADR-0012 A14) | Signature postback verifier, inbox, dedupe and GET-on-notify are Synthetic-tested; reconciliation runs by polling. No webhook registered, no public endpoint | A public HTTPS endpoint; a registered Sandbox webhook; real deliveries verified, deduplicated and replayed; evidence of a forged and a replayed event being refused | Hosting choice and exposure; registering the webhook (a provider configuration change) |
| H2 | **Refunds, reversals, dispute-aware accounting** | Refund **detection** only (`REFUND_OBSERVED` exception); reversal entries exist in the journal model; no refund posting, no dispute handling | Observe a real Sandbox refund's fee treatment; post compensating entries checked by an extended checker v2; partial refunds; disputes and chargebacks as exceptions with holds; capacity effects | A Sandbox refund (a provider mutation) needs separate authorization |
| H3 | **Stronger payer attribution** | Payer source account NOT_VERIFIED; merchant attestation available (ADR-0012 §17.2) | Transaction Search `payer_info` (scope `reporting/search/read`, up to 3 h latency) matched to the payer binding, recorded as provider-reported payer identity; still not proof of the payer's own books | Enable the scope on the REST app; authorize the read-only API use |
| H4 | **Production-grade identity** | Local password plus step-up sessions, TEST identities, membership roles (P4) | External IdP or OIDC, MFA, session and device management, credential rotation, audited admin roles | Identity provider choice; no production use without it |
| H5 | **Operational recovery and monitoring** | Outbox leases, fencing, dead letter, `rearm`, exceptions table; no alerting | Metrics and alerts (dead letters, open exceptions, stale operations, checker failures), runbooks, backup and restore drills, an operator console for exception resolution | Hosting and monitoring stack |
| H6 | **Multi-merchant PayPal onboarding feasibility** | One app, its own Business account as the invoicer (ADR-0012 §2) | Research and Sandbox test of the Partner or Connected-path onboarding and permissions for invoicing on behalf of merchants; consent and fee implications | PayPal program eligibility; Sandbox experiments need authorization |
| H7 | **Actual merchant validation and economic impact** | Synthetic benchmarks only (B0–B5, honest zero and negative lift, D013) | Consenting real merchants, matched baselines, pre-registered metrics, incremental on-time value with confidence intervals | Recruitment, consent, legal review; outside the current scope |
| H8 | **INV-1 timestamp discrepancy** | Reproduced twice (about 8 h); capture time is used for ordering | Confirm the cause with PayPal documentation or support; then label or convert | None (read-only research) |
