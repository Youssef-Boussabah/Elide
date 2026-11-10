# ELIDE — Build Execution Plan (Operational Supplement)
**Version:** 1.0 · 2026-11-10 · **Planning status:** proposed and ready to execute; not evidence of implemented software.
**Authority:** Read `00_START_HERE.md` first. `16_DECISIONS_STATUS_AND_OPEN_QUESTIONS.md` is the source-of-truth decision register. This playbook operationalizes `14_BUILD_ROADMAP.md`; it does **not** override finance, permission, or payment rules in the Project Sources.
**Deadline verified:** November 12, 2026, 12:00 p.m. Pacific Standard Time / 3:00 p.m. Toronto (EST). [Official Devpost rules](https://paypalaihackathon.devpost.com/rules).

## 0. Product and integration decision

- **First release:** a separate, browser-based, PayPal-integrated application, **not** a native PayPal feature or PayPal partner integration.
- **Future possibility:** an approved PayPal merchant-dashboard extension, partner integration, licensed SaaS, or acquired capability. Those require permission, validation, and a commercial agreement; none exists merely because we build an API client.
- **Finance invariant:** AI interprets merchant policy, proposes terms, explains solver output. Deterministic code checks finance, terms, reserves, timing, and consent. Every payer independently approves a specific payment; PayPal externally executes/records its payment. Predicted or provider-confirmed incoming funds are not declared spendable without separate current evidence.
- **Hackathon truth boundary:** one verified PayPal Sandbox invoice/payment is real Sandbox evidence; downstream cascade may use labeled synthetic state/merchant attestations. Never present synthetic settlement or research-only noncash set-off as an actual PayPal payment.
- **Audience:** an SMB buyer/AP approver, supplier/AR approver, downstream merchant, and reviewer/auditor. The anchor buyer must benefit privately, not merely subsidize the payment network.

## 1. Scope frozen for submission

### Required functional journey

1. Load a seeded **synthetic**, permissioned network of 3–4 merchants and four invoices; each obligation has original identity, payer, payee, USD cents, due timestamp, revision, and provenance.
2. Log in as a demo buyer; review available capacity, protected reserve, unknown or held funds, and two supplier early-payment choices.
3. Compare B0–B3 conventional decision baselines with Elide's network-aware choice. Include how much is projected, not confirmed. A same-discount tie must compare against a **random tie-break expected value**, not only the worst option.
4. Enter a natural-language reserve or exclusion instruction; AI returns typed JSON, source span, and request for human confirmation. Altering an approved constraint recomputes candidate feasibility.
5. AI creates **one bounded counteroffer** (e.g., $5 discount insufficient; proposes $10). Supplier can accept/decline. An approved version has explicit expiry, fee cap, and required payer action; AI never impersonates acceptance.
6. The deterministic solver and **separate replay verifier** validate the cash prefixes, reserves, fees, consent, invoice revision, timing, availability and no-double-allocation constraints.
7. Buyer initiates one **PayPal-hosted, sandbox** payer action for an owned/authorized merchant invoice; separately fetch authoritative provider resource, compare amount and currency, and post one immutable financial event.
8. Show plan state, provider-confirmed receipt, audit details, and a deliberately blocked/held dependent leg. Any remaining downstream transfers must be labeled projected or synthetic unless each one is independently executed and verified.

### Hard non-goals before submission

- No autonomous payments; no direct LLM access to PayPal credentials.
- No lending, pooled wallet, bridge funding, legal multilateral netting, instant cross-merchant balance feeds, proprietary credit, or cross-currency payment optimization.
- No claim of trained/validated merchant-behavior ML without real labeled permissioned history.
- No claim that `PAID` webhook implies spendable merchant funds or that gross repeated transfers create liquidity/economic profit.
- No unapproved reissuance or mutation of legally meaningful invoice terms.

## 2. Where we build and deploy

**Primary development:** a local Git working tree (`elide/`) on a stable Linux environment (native Linux or WSL2/Ubuntu on Windows); macOS is supported with equivalent commands. Use a terminal, editor and Docker. Do not couple code to one local operating system.

**Source control:** a dedicated `elide` GitHub repository, with `main` protected as stable and small feature branches: `feat/paypal-spike`, `feat/finance-core`, `feat/planner`, `feat/ai`, `feat/merchant-ui`, `feat/reconciliation`. Make it public with license and runnable instructions before submission. No secret-bearing commits, screenshots or logs.

**Development services:** React/Vite locally; FastAPI locally; PostgreSQL via Docker Compose; a worker/poller from the same Python codebase for durable outbox/inbox; PayPal Sandbox in the cloud. HTTP-only localhost is fine for UI development, but PayPal webhooks require an HTTPS public callback URL (hosted backend or tunnel) and verified metadata. For integration proof, prefer hosted HTTPS backend; when webhook delivery is blocked, provider GET reconciliation must still be demonstrated and the limitation documented.

**Submission demo:** prefer a hosted HTTPS application if stable, **plus** a reproducible local `docker compose up --build` demonstration with sample data and optional PayPal Sandbox secrets. A hosted app is not required by the rules if judges can run the project using complete instructions.

**Hosting caution:** Render supports free preview hosting, but free web services spin down after 15 minutes of idle time and **free PostgreSQL expires 30 days after creation**. A DB created Nov 10 can expire around Dec 10, before judging ends Dec 15. Avoid depending on an unupgraded free database through judging; use a durable paid DB, arrange a documented migration, or rely on a complete clean-clone local setup. Do not use production bank credentials, real customer invoices, or real money.

## 3. Locked tool choices and reasons

| Area | Tool / choice | Why / limitation |
|---|---|---|
| Editor + coding | VS Code or equivalent, Codex CLI as coding assistant, terminal | Assisted development; all changes reviewed and tests run; don't assume tools can act unattended |
| Repo | Git, GitHub, GitHub Actions | Version control, code review, public reproducibility, automated tests |
| UI | React + TypeScript + Vite | Simple SPA with typed API boundary; fast local iteration |
| UI styling | Tailwind CSS + shadcn/ui (optional) | Clear finance controls, accessibility; avoid theme-building over payment work |
| Network visualization | React Flow (`@xyflow/react`) | Clear direction/status of invoice edges; add accessible table fallback |
| Data tables | Basic HTML/React table; AG Grid Community only if time | Avoid tying viability to sponsor integrations; use AG Grid for genuinely useful functionality only |
| API | Python 3.12 + FastAPI + Pydantic | Financial pure functions, request validation, OpenAPI schema |
| Data | PostgreSQL 16+; SQLAlchemy + Alembic | ACID transaction for approvals, immutable revisions and outbox; enforce tenant ownership |
| Async operations | Same repo's DB-backed worker/outbox/inbox | Durable retries without adding Redis/Celery complexity for hackathon |
| Solver | Pure Python exact DFS/enumerator for small fixture; OR-Tools CP-SAT later if needed | Independent exhaustive comparison and honest optimality status for small N |
| AI | A configurable accessible LLM API with JSON-schema output + Pydantic validation | One live meaningful call with human confirmation; provider/key chosen based on actual access, not presumed ChatGPT Plus API entitlement |
| PayPal | Developer Sandbox, Invoicing v2, hosted payer action, REST resource GET, verified webhooks | Merchant-owned payments; no partner-only access assumption |
| API debugging | curl/httpie and Postman | Redacted, repeatable requests and response snapshots |
| Tests | pytest, Hypothesis, FastAPI test client/httpx, Vitest, Playwright if feasible | Unit/property/contract and end-to-end assurance |
| Lint/static | Ruff, TypeScript typecheck, ESLint | Low-friction build and code hygiene |
| Packaging | Docker, Compose, `.env.example`, LICENSE, README, seeded fixtures | Consistent local judge setup |
| Optional hosting | Render or equivalent HTTPS host with persistent PostgreSQL | Stable test URL only if economics and lifetime through Dec 15 work |

**Explicitly excluded as baseline dependencies:** Kubernetes, Kafka, event microservices, blockchain, production payouts, machine-learning infrastructure, vector database, custom GPU model hosting, fraud scoring, automatic credit.

## 4. Repository layout

```text
elide/
├── README.md                  # precise local and hosted demo setup
├── LICENSE                    # open-source license at repository root
├── .env.example               # names/placeholders only; never real keys
├── .gitignore
├── compose.yaml               # reproducible local DB and services
├── pyproject.toml             # pinned backend deps / tool config
├── web/                       # Vite React TypeScript client
│   └── src/
│       ├── features/{overview,opportunities,graph,policies,approvals,execution,evidence}/
│       ├── api/
│       └── components/
├── api/
│   ├── app/                   # FastAPI routes, auth / tenant access, DTOs
│   ├── domain/                # exact cents, invoice/offer/policy revisions
│   ├── liquidity/             # capacity and blocker calculations
│   ├── planning/              # reference optimizer + distinct verifier
│   ├── ai/                    # bounded LLM adapter + schema validation
│   ├── integrations/paypal/   # credentials isolated; provider mapping
│   ├── accounting/            # append-only journal and reconciliation
│   └── workers/               # payment outbox, verified webhook inbox
├── migrations/
├── fixtures/                  # synthetic, deterministic network + labels
├── tests/{unit,property,contract,integration,sandbox}/
├── evidence/                  # redacted logs, manifests, screenshots
└── docs/{ADRs,API,SECURITY,DEMO,SUBMISSION}.md
```

For Python package implementation, use ordinary Python-import-safe actual directories; braces in the layout indicate conceptual groups, not literal required directory names.

## 5. Phase gates and build order

### Phase 0 — Bootstrap, credentials, and PayPal feasibility (immediate; blocking)

**Implement:** Git repo + environment checks, dependency locks, CI skeleton, `.gitignore` and `.env.example`. Create Sandbox business seller and Sandbox personal payer, app credentials, and obtain OAuth token from server-side only. Run a tiny independent PayPal Invoicing v2 spike: create a draft under owned Sandbox merchant, send it to a Sandbox payer, observe hosted link, attempt actual payer completion, retrieve invoice/payment state. Test whether the exact discounted-invoice amendment workflow works for the account, without assuming partner scopes.

**Pass:** Evidence shows credentialed API access, a created invoice, and a confirmed supported path to a hosted payment. Record redacted HTTP status, resource IDs and exact documented endpoint. Do not declare a real payment completed until independently observed.

**Fallback:** If Invoicing cannot support the desired flow, use PayPal Orders v2 hosted approval/capture for a **separately owned application invoice**, documenting that it is **not** a PayPal Invoicing invoice and that local accounting tracks the authorized obligation. If no actual Sandbox payment works, flag a major submission risk immediately rather than fake integration.

### Phase 1 — Financial kernel and seed data (must pass before solver)

**Implement:** exact integer USD cents; UTC timestamps plus merchant TZ; immutable original invoice identity/revision; reserves, fees, expiry, disputes, consent; capacity with observed/held/attested/unknown classifications. Seed canonical buyer $1,490/$500 reserve / two $990 choices and $900 + $800 downstream; plus AI negotiation scenario with initial $5 then proposed $10 discount.

**Pass:** independently checked invariants, cash conservation in fee-free example, no reduction below reserve, no unpaid invoice mislabeled paid, and no hypothetical incoming balance treated as spendable. Unit tests include held $990 receipt, supplier $150 reserve shortfall $60, and duplicate obligation.

### Phase 2 — Deterministic planner, verifier and fair benchmarks

**Implement:** small exact planner over candidate invoice/slot/offer choices. Separate independently coded replay verifier. B0 no acceleration, B1 earliest-due, B2 buyer-discount-only, B3 random tie expected, B4 network-aware and B5 with approved negotiated change. Compute money committed early, buyer benefit net of fees/time cost, projected face, incremental by baseline, cash-dollar-days. Report `OPTIMAL` only when exhaustive or proven by solver.

**Pass:** planner equals brute-force reference on small networks; no double-spend or same-slot cash reuse; deliberate infeasible policy returns explicit binding constraints. Measured synthetic results must report `0` or negative lift as honestly as positive cases.

### Phase 3 — Bounded LLM intelligence

**Implement:** policy parser with typed fields and source spans; merchant confirmation UI and safe ambiguity handling; one bounded counteroffer for draft discount/date terms with input redaction. LLM receives no PayPal credentials or payment execution tools. Deterministic code validates proposal and returns explanation on rejection.

**Pass:** confirmed reserve constraints never relaxed by malicious invoice text, ambiguous dates abstain, unconfirmed policy does not bind execution, generated proposal cannot mutate PayPal invoice or produce merchant consent. Log model/version and prompt version, with no private prompt content exposed to other tenants.

### Phase 4 — Merchant access, UX and independent approvals

**Implement:** buyer and supplier demo roles through distinct authenticated sessions; per-merchant API authorization and object checks. Screens: Today/Liquidity, Opportunities comparison, Graph Explorer + table, AI Policy Review, Negotiation Room, Execution, Books/Evidence. Every decision bound to immutable invoice/offer digest and explicit counterparty/amount/expiry. Show scenario state labels `SIMULATED`, `PROPOSED`, `APPROVED`, `PAYPAL_CONFIRMED`, `FUNDS_USABLE_ATTESTED`, `BLOCKED` distinctly.

**Pass:** two separate merchant approvals demonstrable; unauthorized actor cannot view another merchant's private reserves or authorize its payment; stale plan/terms return conflict, no action button for unapproved terms; demo works at laptop resolution and via keyboard.

### Phase 5 — Reliable PayPal execution and reconciliation

**Implement:** create a payer-hosted Sandbox action only after strict merchant approval; persisted payment intent + outbox atomically; provider resource GET, independently verified webhook signature/event ID and inbox dedupe, proper partial/failed/refunded/unknown statuses, immutable accounting entries. Payment provider confirmation and spendable capacity are separate states. Failure stops dependent leg and retains completed history; no automatic rollback/recharge.

**Pass:** at least one **actual** sandbox payer action and provider-confirmed receipt at the correct amount/currency; duplicate webhook or repeated request yields one economic posting; timeout leaves `UNKNOWN` for lookup rather than causing a blind new charge; held receipt blocks dependent payment.

### Phase 6 — QA, benchmarking, security and evidence

**Implement:** seeded fixtures + 10 adversarial scenarios: reserve breach, held receipt, fees, partial payment, duplicate webhook, expired consent, tenant leak, stale revision, currency mismatch, unreachable or disconnected chain, plus prompt injection and concurrency. Exact-solver comparisons, financial trace, synthetic benchmark table, redacted provider evidence manifest and security review. Verify clean install from Git clone with only documented prerequisites.

**Pass:** financial and consent invariants have zero violations; tests/CI green; distinct labels for plan projection versus Sandbox-confirmed payment; financial metrics clearly say whether computed, simulated or observed. Do not invent a percentage lift absent a valid benchmark run.

### Phase 7 — Deployment, demo, open-source submission (release gate)

**Implement:** README with quickstart, synthetic demo user accounts, API key setup without publishing secrets; OS-independent install; hosted URL if stable; open-source license; signed-off code release/commit SHA; <3-minute video showing real UI and PayPal payment; Devpost description/tools used. Public links accessible signed out. Maintain working judging access through **December 15, 2026**.

**Pass:** working application can be run from clean clone or hosted; public repo license visible; actual PayPal and live AI operations are demonstrable; YouTube public and <3 minutes; submit before Thursday **Nov 12 3 p.m. Toronto**, leaving a substantial contingency buffer. Save submission receipt.

## 6. Schedule and priority checkpoints (Toronto)

### Tuesday, Nov 10 — External-risk-first foundation
- P0: immediately verify Sandbox merchant account, API token, invoice create/send and actual hosted buyer-payment feasibility.
- Bootstrap repo, dependencies, local DB and migration baseline.
- P1: exact financial primitives, seed data, basic reserve and fee tests.
- If time allows, P2 first deterministic comparison response and a bare UI showing it.
- **Gate at end of day:** PayPal path real/feasible, and no math/data-model showstopper. Do not spend hours making UI beautiful before this gate.

### Wednesday, Nov 11 — Complete vertical product slice
- P2 small exact planner and independent verifier; baseline benchmarks.
- P3 one meaningful live LLM policy/counteroffer invocation with owner confirmation.
- P4 minimal buyer/supplier journey and strict API authorization.
- P5 connect the verified Sandbox payment path and immutable reconciliation.
- Capture an early backup screen recording and receipt evidence before fine UI polish.
- **Gate at end of day:** end-to-end critical path works from buyer decision through sandbox payment to independently fetched status.

### Thursday, Nov 12 — Stabilize, publish, submit
- Morning: P6 tests, error cases, clean clone, repo license and demo state reset.
- Freeze scope; remove or clearly mark any optional feature not actually working.
- Record/upload final YouTube demo, finish Devpost description and verify external links while signed out.
- Target **submission earlier than the 3 p.m. EST deadline**, not at the last minute.
- Confirm receipt and hosted/reproducible access; leave the judging setup working until Dec 15.

## 7. Scope triage if behind

**Never cut:** exact money accounting; reserve, consent and invoice identity checks; one real PayPal Sandbox payment; provider GET verification; private merchant access; truthful simulated status labels; runnable repository; public video/submission.

**Simplify next:** UI animation, multi-page wizard, complex charts, advanced offer search, additional models, production-grade SSO, robust graph styling, extra merchant examples.

**Defer completely:** graph neural networks, real predictive ML trained on synthetic labels as if commercial, legal debt netting, autonomous lending or auctions, cross-currency support, PayPal partner onboarding beyond allowed scopes, complex risk attribution, advanced ZK cryptography, Kubernetes/streaming infra.

## 8. Security + development rules (apply to each phase)

1. API secret, webhook ID, demo passwords, provider tokens go into a **local untracked environment store**; `.env.example` shows placeholders only. Never paste secrets into a public chat, GitHub issue or tracked artifacts.
2. Do not call PayPal APIs from LLM tool instructions or browser using server credentials; only backend adapter calls documented, authorized operations.
3. Every plan and payment intent has actor, tenant, invoice revision, plan digest, amount, currency, fee ceiling, expiry and correlation/idempotency key.
4. Use transactions for local reservations and outbox writes. Network API calls occur outside DB locks. Provider event may be duplicated, delayed or reversed.
5. An invoice `PAID` event is not proof of immediately spendable money; user attestations are marked as attestations and expire.
6. If a completed leg is followed by an impossible next leg, keep the completed transfer; block future actions, reconcile, generate a new plan and obtain new approvals. Never claim atomic rollback across independent merchants.
7. Keep every synthetic datum tagged synthetic; audit and presentation numbers refer to their correct source (design, synthetic test, Sandbox, or real consenting merchant outcome).

## 9. Acceptance / evidence matrix

| Demonstrated user outcome | Implementation evidence required | Status at writing |
|---|---|---|
| Buyer protects reserves | Unit/property tests for capacity and time prefixes | Planned |
| Two offers compared fairly | B0–B5 seeded baseline results and replay | Planned |
| AI turns text into safe rule | Live output + typed schema + confirmed UI + injection test | Planned |
| AI counteroffer changes feasibility | Proposed revision + independent verifier + supplier approval | Planned |
| Payer approves exact payment | Revision-bound actor record and amount/currency/fee check | Planned |
| PayPal accepts an actual payment | Provider request, redacted invoice and fetched receipt, timestamps | **Unverified** |
| Duplicate/failed event is safe | Inbox/outbox replay and journal test | Planned |
| Judge can run product | Clean-clone commands / hosted functioning URL + public license | Planned |
| Claim of real merchant ROI | Merchant pilot with defensible counterfactual | **Not in hackathon scope** |

## 10. Phase ownership and collaboration

For a solo implementation, the human project owner controls PayPal accounts, secrets, browser approvals, deployment credentials and Devpost submission. AI coding assistance can generate patches, tests, documentation and fixture data but must not be given payment authority. Use one workstream at a time in dependency order: **PayPal proof → financial kernel → planner → policy/approval → payment integration → QA/demo**. If teammates contribute, split finance and PayPal lanes first; require shared typed contracts and CI before merge.

Suggested development issue labels: `P0-provider`, `P1-domain`, `P2-planner`, `P3-ai`, `P4-ux`, `P5-execution`, `P6-evidence`, `P7-release`, `blocker`, `financial-invariant`. Each pull request includes: what changed, assumptions, tests run, impact on both buyer/supplier incentives, PayPal feasibility, and which evidence class can now be claimed.

## 11. Bootstrap commands (examples, not already executed on the user's machine)

```bash
# Linux or WSL2 shell: inspect prerequisite tools first
git --version
python3 --version
node --version
pnpm --version
docker compose version

# Existing repo or newly created project tree; do not create a second remote accidentally
mkdir -p elide && cd elide
git init
mkdir -p api/{app,domain,liquidity,planning,ai,integrations/paypal,accounting,workers} \
  fixtures migrations tests/{unit,property,contract,integration,sandbox} \
  evidence docs web

# Actual project dependencies, docker services, lockfiles and environment template
# are added in Phase 0. Keep PayPal Client Secret in an untracked local .env.
```

Repository starter verification before publishing:

```bash
git status --short
git ls-files | grep -Ei '(^|/)(\.env$|.*secret.*|.*token.*)' || true
# Run backend tests and frontend typecheck once scaffolds exist.
```

## 12. Questions resolved by test rather than speculation

- Is the chosen merchant-owned Sandbox invoice discount workflow allowed and usable? **P0 experimental test**.
- Does the sandbox payer actually complete and does provider GET match expected amount? **P0/P5 evidence test**.
- Can current merchant funds be verified spendable before dependent payment? **No assumption**; simulate or attest downstream separately until verified.
- Is preferred model API accessible? **P3 test**; ChatGPT subscription does not imply included external API credentials.
- Can the chosen host remain available through Dec 15? **P7 infrastructure test and budget commitment**.
- Is Elide truly economically better than naive buyer-only discount optimization? **P2/B0–B5 matched experiments**, not an anecdote.

## 13. Current status and first action

**As of this plan:** specifications exist; no Elide application, deployed service, production optimizer, trained merchant ML model, or real PayPal Sandbox receipt has been independently provided in the Project Sources. The first execution action is **Phase 0: create/identify the working repository, verify local dependencies, and attempt a minimal merchant-owned PayPal Sandbox invoice/payment end-to-end**. Only then mark P0 complete with redacted evidence.

### External sources checked Nov 10, 2026

- Hackathon rules and date: https://paypalaihackathon.devpost.com/rules
- PayPal Invoicing API quickstart: https://developer.paypal.com/api/invoicing
- PayPal selected-partner integration limitation: https://developer.paypal.com/platforms/invoicing/integrate
- PayPal Orders v2 fallback path: https://developer.paypal.com/api/rest/integration/orders-api
- PayPal webhook delivery/verification: https://developer.paypal.com/api/rest/webhooks/rest/
- Render free-service and free-Postgres limitations: https://render.com/docs/free
