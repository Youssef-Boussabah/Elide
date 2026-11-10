# 17 — Source Register, Provenance and Claim Boundaries
**Verified against publicly accessible source listings on 2026-11-10 where indicated.** Cite the original external source in public pitches; do not use this list as proof that Elide achieved the external results. Provider documentation can change.

## Primary external evidence
| ID | Source | What it supports | Critical limitation |
|---|---|---|---|
| S01 | Intuit QuickBooks, *2026 Small Business Late Payments Report*, https://quickbooks.intuit.com/r/small-business-data/small-business-late-payments-report-2026/ | Survey: 59% invoices 30+ days overdue; 49% payment processing cash-flow gaps; 42% outside pressures delaying own vendor payments; 74% not fully automated bill-pay | Survey population and questions; not PayPal merchant-specific or evidence Elide works |
| S02 | Fleischman, Dini & Littera, *Liquidity-Saving through Obligation-Clearing and Mutual Credit* (2020), https://doi.org/10.3390/jrfm13120295 | Sardex internal-debt reduction about 25% clearing alone / 50% with mutual credit | Debt clearing and credit mechanisms **not** same as accelerated PayPal invoice cash flows |
| S03 | Bank for International Settlements Working Paper 1318 (Dec 16 2025), https://www.bis.org/publ/work1318.htm | Auction-based liquidity-saving design, compensation and wholesale system simulation; 79% average auction success; 2.6–12.2% liquidity savings | Wholesale RTGS banks, not SMB invoice product or PayPal pilot outcome |
| S04 | PayPal AI Hackathon Official Rules, https://paypalaihackathon.devpost.com/rules | Deadline, viability, equally weighted five criteria, working app, public repo/license, demo video and sandbox/AI requirement | Current competition rules, not PayPal commercial product policy |
| S05 | PayPal Developer *Integrate invoicing*, https://developer.paypal.com/platforms/invoicing/integrate | Multiparty invoicing available to selected partners, onboarding requirements `INVOICE_READ_WRITE` and `ACCESS_MERCHANT_INFORMATION` | Merchant-owned standalone invoice support may differ by account/app/operation |
| S06 | PayPal Developer *Invoicing*, https://developer.paypal.com/docs/invoicing/ | Invoicing REST/hosted invoice concepts | Must test exact endpoint and app privileges |
| S07 | PayPal Invoicing v2 update, https://developer.paypal.com/api/invoicing/v2/invoices-update/ | Schema and invoice-update terms | Behavior/discount acceptance for chosen account must be sandbox verified |
| S08 | PayPal REST Webhooks, https://developer.paypal.com/api/rest/webhooks/rest/ | Event verification and notification mechanics | Webhook received ≠ spendable cash |
| S09 | PayPal REST idempotency, https://developer.paypal.com/api/rest/reference/idempotency/ | Request identifiers and retry safeguards | Endpoint-dependent support and retention |
| S10 | Cycles, *Cycles Raises $8.7 Million* (May 21 2026), https://www.cycles.money/blog/cycles-raises-8-7-million | Reported funding and privacy-preserving clearing/network ambitions | Company-supplied report, not evidence of achieved merchant-scale product impact |
| S11 | Cycles, *Cycles is moving to Arc* (Jun 17 2026), https://www.cycles.money/blog/cycles-is-moving-to-arc | Described moving business/stablecoin clearing network | Company strategy/claim, not audited adoption evidence |
| S12 | J.P. Morgan dynamic discounting, https://www.jpmorgan.com/payments/solutions/trade-and-working-capital/payables-finance/dynamic-discounting | Dynamic-discounting precedent | Commercial product details may change |
| S13 | PayPal AI Hackathon overview, https://paypalaihackathon.devpost.com/ | Event scope, official submission summary and judging overview | Full official rules authoritative for detailed eligibility |

## Secondary areas to verify when needed
- Current PayPal public merchant strategies and financial results via PayPal Investor Relations (date each disclosure; do not infer an announced product from strategic alignment).
- SAP Taulia and C2FO official product pages for early payment/discounting and supply-chain finance comparisons.
- Google OR-Tools official guides for exact CP-SAT model constraints, bounds and solver result states.
- Legal counsel/accountant documentation for multilateral set-off and invoice concessions in pilot jurisdiction.
- Verified PayPal Balance Reporting API documentation for timing and information latency; do not treat a statement balance as an instant spendability guarantee.

## Evidence statements to use or avoid
**Acceptable:** 'There is survey evidence of late-payment and payment-processing pain.'
**Not acceptable:** '59% of PayPal's customers need Elide.'
**Acceptable:** 'Academic work demonstrates obligation-clearing and auction-based liquidity saving in other systems.'
**Not acceptable:** 'Elide saves 25–50% liquidity or will have 79% success.'
**Acceptable:** 'We designed a proof-of-concept to test downstream-aware early payment decisions.'
**Not acceptable:** 'Our system has already proven merchant ROI or unrestricted merchant data access.'

## Bibliography maintenance
As research develops, add source, publication date, access date, affected claim, specific sample/system and methodological limits. Prefer official documentation or original papers to promotional summaries. For any quantitative hackathon claim, record corresponding code commit, test seed and exact measurement procedure.
