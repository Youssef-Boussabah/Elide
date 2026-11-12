# P4 browser workflow check (2026-11-11, Claude in Chrome, local only)

- Environment: Windows 11, Chrome; FastAPI `127.0.0.1:8000` + Vite `127.0.0.1:5173` (proxy
  `/api`), local PostgreSQL 16 at head `0004_merchant_platform`.
- Data: `workspace-seed-v1` (SYNTHETIC network, TEST identities). Passwords were generated
  locally into the git-ignored `.local/demo_credentials.json`; none appear here.
- Evidence class: **Synthetic-tested**, browser observation. No PayPal call, no model call,
  no money moved.

| Step | Session | Observation |
|---|---|---|
| 1 | buyer owner | Sign-in; header shows TEST IDENTITY + SYNTHETIC MERCHANT; liquidity USD 990.00 deployable (1,490.00 spendable, 500.00 reserve), basis SYNTHETIC with observed/valid-until timestamps |
| 2 | buyer owner | Opportunities: NEGOTIATE; food_001 blocked ("needs >= USD 10.00, terms give USD 5.00"); explanation from deterministic template (no model) |
| 3 | buyer owner | Counter: "Suggest a bounded discount" returned USD 10.00 (deterministic bounds); counter sent; offer shows "Recorded: buyer (accept)" only |
| 4 | supplier owner (separate login) | Same offer phrased for the supplier ("you would receive / you give up"); Accept; history shows agreed with both parties' decisions |
| 5 | supplier owner | `GET /api/v1/m/buyer/overview` in the browser: `404 NOT_FOUND` (identical to a nonexistent merchant) |
| 6 | supplier owner | Audit page: hash-chained events (offer.proposed / countered / agreed, membership.granted) |
| 7 | buyer owner | Plan & approvals: leg food_001 r4 USD 990.00 in force; confirmation states exact amount, payee, revision, fee ceiling, expiry; approved |
| 8 | buyer owner | **Defect found:** re-opening the page re-planned (new plan id) and showed the leg as pending again. Fixed: decisions carry over for identical live terms; regression test added |
| 9 | buyer owner | Prepare payment intent: INTENT CREATED NOT SENT, "held: no PayPal call"; overview committed USD 990.00, deployable 0.00 |
| 10 | buyer owner | **Defect found:** with the cash committed, the re-plan reported food_001 as blocked. Fixed: invoices in execution are not re-planned; Opportunities now NO_ACTION; regression test added |
| 11 | buyer owner | Keyboard: first Tab shows "Skip to content"; active nav item marked (`aria-current`); no console errors on Opportunities or Network |
