# fixtures/

Deterministic, **synthetic** financial scenarios. Nothing in this folder is PayPal
evidence. Real Sandbox facts stay in `evidence/paypal-sandbox/` and are loaded through
`api.integrations.paypal.settlement.load_settlement`. Downstream legs are **simulated** and
never provider-confirmed.

## `canonical.py` (fixture version `canonical-v1`)

All objects carry `SourceKind.SYNTHETIC_FIXTURE` provenance or `EvidenceKind.SYNTHETIC`
capacity evidence. Synthetic evidence deploys cash only when a caller passes
`allow_synthetic=True` (simulation mode). The synthetic clock is `AS_OF = 2026-11-10T15:00Z`.

| Scenario | Builder | What it represents | Expected result (tested) |
|---|---|---|---|
| A: normal cascade | `scenario_a()` | Buyer $1,490 cash, $500 reserve. Two $1,000 invoices (food, packaging), each with an authorized $10 early-payment discount. Food supplier owes carrier $900; carrier owes mechanic $800 | Buyer deploys exactly $990; only one of the two invoices can be funded; fee-free chain closes Buyer $500, Supplier $90, Carrier $100, Mechanic $800 (= $1,490 conserved); gross flow $2,690; downstream face $1,700 |
| B: negotiation | `scenario_b_supplier_offer()`, `scenario_b_proposed_ten()`, `scenario_b_authorize()` | Supplier offers $5 (draft); buyer requires ≥ $10 (`BUYER_MIN_DISCOUNT`); a proposed $10 is a draft until both parties authorize | $5 below minimum, counts $0; proposed $10 counts $0 (paying $990 leaves $10 open); authorized $10 settles with $990; expired proposal cannot be authorized |
| C: held receipt | `scenario_c()` | Supplier with $0 spendable, $150 reserve and a held $990 receipt | Deployable $0; receipt only projected |
| D: reserve shortfall | `scenario_d_fee_free()`, `scenario_d_hypothetical_usd_net()` | Supplier verified $990, owes $900, $150 reserve | Deployable $840, short $60. Hypothetical USD net after the real $29.01 fee ($960.99): short $89.01. The actual CAD settlement is currency-blocked |
| E: malformed/duplicates | tests only | Duplicate invoice identity, conflicting revisions, stale evidence, invalid amounts | All rejected or counted once |

Tests: `tests/unit/scenarios/test_canonical_scenarios.py`.
