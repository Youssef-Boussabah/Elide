# P4 browser end-to-end regression (Playwright, 2026-11-11)

## Setup

| Item | Value |
|---|---|
| Command | `cd web; pnpm e2e` |
| Browser | `@playwright/test` 1.56.1 driving the installed Google Chrome (`channel: "chrome"`); no browser binary downloaded |
| Stack | FastAPI on `127.0.0.1:8001` and Vite on `127.0.0.1:5174`. A fresh database `elide_e2e` is migrated and seeded by `scripts/e2e_setup.py` |
| Data | SYNTHETIC `workspace-seed-v1` network with TEST identities. Passwords go to the git-ignored `.local/e2e_credentials.json` |
| External calls | No PayPal call, no model call, no money movement |

## Result

Local run on Windows 11 with Chrome: **5 passed**.

| Test | Covers |
|---|---|
| 1 | Bad credentials get a generic message |
| 2 | Viewing Opportunities three times, with reloads, creates no durable plan (`GET /plans` count unchanged) |
| 3 | The buyer counters with a $10 bounded suggestion. The supplier, in a separate browser context, sees "You give up USD 10.00" and accepts. The supplier session gets a 404 for the buyer's API and is redirected away from the buyer's routes. The buyer creates a plan explicitly, approves exactly USD 990.00 to food_supplier, and prepares an intent: "held: no PayPal call", "Money moved: no" |
| 4 | The supplier has no executable leg while the upstream payment is in execution |
| 5 | The buyer confirms a $700 reserve on the Policy page; the earlier approval then shows "CONDITIONS CHANGED: RE-APPROVE" and never "APPROVED" |

## Defects found by these tests

Each was fixed and is covered by a test.

1. **The approval-time verifier falsely rejected legs on a moving clock.** The planned slot was mapped onto a fresh clock and became "early". Fixed in `plans.verify_leg_now`: an on-due-date leg is checked at its current due slot. This was invisible with the frozen clock of the API tests.
2. **The counteroffer confirmation disappeared.** The decided offer's card unmounted on reload. The confirmation now lives at page level and is covered by the jsdom and E2E tests.
3. **An already-confirmed policy draft still offered "Confirm".** The API now returns `confirmed_revision`, and the UI hides the button.

## CI

The same suite runs as the `e2e` job in GitHub Actions.
