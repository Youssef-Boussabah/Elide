# 03 — PayPal Strategic Fit and Feasibility

## Why PayPal is relevant
PayPal's merchant network, invoicing tools, hosted payment experiences, sandbox developer tools and increasingly AI-oriented merchant strategy create a plausible distribution/infrastructure context for Elide. Elide does not seek to replace PayPal settlement; it seeks to become a **decision and coordination layer** that produces approved payment intents, and uses PayPal to collect evidence of completion.

This is a proposed adjacency, not proof that PayPal would commercialize it.

## Value for PayPal (commercial hypotheses)
- **Merchant engagement:** an accounts-payable/receivable workflow gives merchants a reason to open PayPal beyond the moment of checkout.
- **Potential incremental payments:** some accelerated obligations might be processed through PayPal where they otherwise wouldn't. Distinguish switched volume from new payment volume and accelerated timing.
- **Value-added software:** workflow analytics, planning, permissions, and audits might support merchant service subscriptions. Pricing has not been tested.
- **AI strategy:** use AI for bounded interpretation and negotiation in financial decisions; retain deterministic controls and human payment authority.

A commercial contribution model should be: incremental retained SaaS revenue + provider contribution margin from *truly incremental* volume − acquisition/onboarding/support/compliance/risk cost − cannibalization. Gross processed dollar volume is neither revenue nor profit. A clearing technique that removes transfer volume may improve merchant experience while reducing payment-processing monetization; quantify the trade-off.

## PayPal integration reality
- **Invoice API:** supports common invoice lifecycle operations through authorized accounts. Hosted payer experience may provide the direct merchant-owned action.
- **Multiparty partner invoicing:** PayPal's current platform integration states availability to **select partners**, with specific partner onboarding and feature permissions such as `INVOICE_READ_WRITE` and `ACCESS_MERCHANT_INFORMATION`. Do not infer unrestricted access to other merchants' invoices. See https://developer.paypal.com/platforms/invoicing/integrate
- **Webhooks:** event arrival is not unambiguous settlement/funds-availability proof. Verify signatures through supported process, fetch authoritative resource, reconcile exact amounts and status. The event may be late, partial or duplicated.
- **Funds availability:** PayPal payment success, account balance reporting, and unrestricted funds available for the next merchant are distinct conditions. Reporting lag prevents using reports as the sole instant spendability feed. For the demo, merchant-issued **signed simulation/attestation** is allowed but must be visibly simulated, not called live PayPal spendability.
- **Idempotency:** support and retention vary per operation; verify endpoint-specific semantics, persist operation identifiers and reconcile after uncertain network timeouts.

## Scope of first sandbox implementation
1. Create or select an invoice *owned by an accessible sandbox merchant*.
2. Apply appropriately approved invoice terms via a documented supported update path; if discount fields fail, use clearly approved source-revision path or another documented hosted payment flow.
3. Display hosted payer view; have a sandbox payer actually pay.
4. Query provider state and reconcile amount, currency, invoice, concessions, fees and timestamps.
5. Record whether any downstream payment is another **independently executed sandbox payment**, or only a projected/simulated one.

## Critical permissions tests
For each operation document (a) merchant owner, (b) sandbox app/client, (c) required permissions, (d) request/response, (e) observed state, (f) evidence date. A `403` is an integration access boundary, not a reason to simulate an API call and report it as real. Never store tokens or real merchant payer links in Project Sources.

## Source of truth model
- **PayPal** owns external payment receipt facts.
- **Merchant** owns current liquidity capacity attestation, approvals, reserves and acceptance terms.
- **Elide database** owns proposed plans, decisions, plan versions, journals, and exceptions.
- **ML/LLM** provides predictions or proposals, never authoritative funding evidence.

## Hackathon relevance
Official rules require central PayPal developer platform integration **and** AI, a working interactive app, open-source public code and a public video under three minutes; see https://paypalaihackathon.devpost.com/rules. Do not optimize only for slides or a theoretical pitch.
