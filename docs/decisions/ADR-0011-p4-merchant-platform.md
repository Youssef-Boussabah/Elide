# ADR-0011 — P4 merchant platform: identity, tenant isolation, durable workflow and consent

- **Status:** accepted for implementation (2026-11-11). Pending owner architectural review.
- **Phase:** P4, branch `feat/p4-merchant-platform` from `main` at `3100091`.
- **Sources read:**
  - CLAUDE.md / AGENTS.md;
  - 00 (start here), 16 (decision register), 08 (architecture), 09 (API contracts), 10 (PayPal runbook), 11 (security), 13 (UX), 14 (roadmap);
  - ADR-0005 (PayPal spike) and ADR-0008 to ADR-0010.
- **Supersedes:** nothing. P1–P3 contracts are reused unchanged.

## 1. Context

P1–P3 delivered the financial kernel, the exact planner with its independent verifier, and bounded AI. All of it runs over in-memory synthetic networks: no persisted users, merchants, offers, plans or decisions; no authentication; P3 confirmations are labelled `synthetic_demo_confirmation`.

P4 must make those capabilities usable by separate merchants without weakening D003 (network benefit never overrides private acceptance), D004 (no model authority), D005 (deterministic verification before execution) or D006 (paid ≠ spendable).

## 2. Phase boundary (dependency-based)

The phase tables differ: CLAUDE.md and the tracker put sessions, approvals and isolation in P4 and the live Sandbox payment in P5; doc 14 calls P4 "reliable execution". The owner's P4 brief asks for identity, persistence, consent, recommendations and UX, plus the execution *boundaries*. It explicitly forbids another Sandbox payment without separate authorization.

Decision: P4 delivers identity, authorization, persistence, consent and the payment-intent preconditions, together with the outbox and inbox schema. Live execution — an actual payer action, the provider GET, worker dispatch and webhook signature verification against PayPal — stays in P5, because it depends on authenticated, digest-bound approvals that do not exist until P4 is complete.

P4 is not complete until these are verified:
- identity;
- authorization;
- persistence;
- tenant isolation;
- approval correctness.

The doc-14 execution criteria (a real Sandbox payment, a duplicate event yielding one posting, a hold blocking the next leg) remain P5 gates. The kernel already tests the duplicate-posting and hold rules.

## 3. Authentication: options evaluated

| Option | Assessment | Decision |
|---|---|---|
| Hosted OIDC IdP (Auth0, Okta, WorkOS, Clerk) | Best production answer: MFA, SSO, breach detection. Needs an owner account, a paid or trial service and data-processing review, none of which is authorized | Production target. The session layer is the seam: an OIDC callback creates the same server session |
| Self-hosted Keycloak | Mature OIDC server, but a heavy JVM service beside a modular monolith; operating burden without a requirement | Not now |
| FastAPI-Users | In maintenance mode upstream; its JWT strategies are stateless and hard to revoke | Rejected |
| JWT bearer tokens in browser storage | Exposed to XSS; revocation needs a deny-list anyway | Rejected |
| **Server-side opaque sessions in PostgreSQL + Argon2id passwords (`argon2-cffi`)** | OWASP Session Management, Password Storage and CSRF guidance; immediate revocation; no new service; works across many app instances because state is in PostgreSQL | **Chosen** for P4 |

Details:
- **Passwords.** Argon2id through `argon2-cffi` with its default parameters (RFC 9106 low-memory profile). Hashes are re-hashed on login when the parameters change. A dummy hash is verified for unknown logins so response time does not reveal which accounts exist.
- **Session token.** 256 random bits (`secrets.token_urlsafe(32)`), carried only in an `HttpOnly`, `SameSite=Strict`, `Path=/` cookie. It is `Secure` with the `__Host-` prefix outside local/test. The database stores only the SHA-256 of the token. Login always creates a new session, which prevents fixation; logout revokes it.
- **Expiry.** 30 minutes idle, 8 hours absolute, both server-enforced on every request.
- **Disabling.** A disabled user or a revoked membership takes effect on the next request. Nothing is cached in the browser.
- **CSRF.** Every state-changing request needs `X-CSRF-Token` equal to the session's synchronizer token; only its hash is stored. Any `Origin` header present must be an allowed origin. Login, which has no session yet, requires an allowed `Origin`. `SameSite=Strict` is defence in depth, not the control.
- **Step-up.** Decisions that change money or terms require a password check within the last 10 minutes (`401 REAUTH_REQUIRED` otherwise). This covers confirming a policy, accepting, rejecting or countering an offer, and authorizing a payment leg. Re-authentication is done with `POST /v1/auth/reauth`.
- **Rate limits.** Both are held in PostgreSQL, so they hold across instances:
  - failed logins: 5 per login and 20 per client address in 15 minutes, then `429`;
  - state-changing requests: a fixed-window limit per session.
- **Test identities.** Demo users carry `is_test_identity = true`. The UI labels them **TEST IDENTITY**. The seed command refuses to run outside `local`/`test`/`ci`. Passwords are generated randomly per seed and written to the git-ignored `.local/demo_credentials.json`; they are never committed or printed in logs. These identities are not evidence of real merchant authentication.

## 4. Authorization model

- **Principal.** Resolved only from the session cookie on the server. No request body or header names a user.
- **Merchant scope.** The merchant comes from the URL (`/v1/m/{merchant_id}/...`). It is a resource selector, never an assertion: the server requires an active membership of the principal in that merchant. A missing or foreign merchant returns the same `404` as a merchant that does not exist, so cross-tenant existence is not revealed.
- **Roles → permissions** (doc 11 roles plus an owner role):

| Permission | owner | approver | policy_editor | auditor | viewer |
|---|---|---|---|---|---|
| `workspace:read` (overview, opportunities, network, plans, offers) | ✓ | ✓ | ✓ | ✓ | ✓ |
| `policy:edit` (draft, validate, confirm own policy) | ✓ | | ✓ | | |
| `offer:propose` | ✓ | ✓ | ✓ | | |
| `offer:decide` (accept / reject / counter / withdraw) | ✓ | ✓ | | | |
| `payment:authorize` (approve own plan leg) | ✓ | ✓ | | | |
| `audit:read` | ✓ | ✓ | | ✓ | |
| `members:manage` (revoke membership) | ✓ | | | | |

- **Object-level checks.** These run in the services, not only in the routes:
  - an offer is visible only to its two invoice parties;
  - an invoice is visible to its debtor and creditor;
  - a plan is visible only through `merchant_view` for a participant;
  - an audit event is visible only to its own merchant scope.

  Each check lives in one function per object type.
- **Revocation.** `membership.revoked_at` is set (never deleted). Revocation applies immediately to every session of that user for that merchant. Unexecuted decisions recorded by the user stay recorded as history. A payment authorization made before revocation must be re-checked before any intent (§7).

## 5. Persistence (migration `0004_merchant_platform`)

All money is `BIGINT` cents with `CHECK`s; all times are `timestamptz`. Business facts are append-only, with UPDATE/DELETE refused by a trigger: obligations, revisions, decisions, plan snapshots, policy revisions and audit events.

| Table | Purpose / key constraints |
|---|---|
| `app_user` | login (unique, normalized), Argon2id hash, `is_test_identity`, `disabled_at` |
| `merchant_org` | merchant id (same id space as the domain), timezone, `is_synthetic` |
| `membership` | user × merchant × role; partial unique active membership; `revoked_at ≥ granted_at` |
| `auth_session` | token SHA-256 PK, public `session_id`, CSRF hash, `authenticated_at`, idle/absolute expiry, `revoked_at` |
| `auth_attempt`, `rate_limit_window` | login throttling and the per-session mutation limit |
| `network`, `network_participation` | permissioned network membership; `share_aggregates` consent for aggregate disclosure |
| `obligation` | immutable identity: debtor ≠ creditor, face > 0, currency, provenance, provider ref unique |
| `obligation_revision` | immutable P1 revision (DRAFT offer / AUTHORIZED terms / REJECTED). PK (obligation, revision_no); `supersedes` FK to the same obligation; expiry only on DRAFT; `authorized_by` ⊆ {debtor, creditor} |
| `policy_draft`, `policy_revision` | the draft (source text, validation report, digest, model metadata if any); the confirmed revision (rules, digest, confirming user/session). Unique (merchant, revision_no) |
| `offer` | workflow wrapper around one DRAFT revision: proposer, counterparty, base revision, cash, concession, currency, due, expiry, `terms_digest`, parent offer, status. Partial unique: one open offer per obligation |
| `offer_decision` | merchant × offer decision bound to `terms_digest`; unique (offer, merchant); records user and session |
| `plan_snapshot` | immutable plan record (P2 `elide.plan.v1`) + input/network digests + verifier result; private, read only through scoped views |
| `payment_authorization` | the payer's approval of its own plan leg, bound to plan id/digest, option, obligation revision, payee, amount, fee ceiling, currency, expiry; unique (plan, option) |
| `payment_intent`, `outbox_message` | P5 execution records, created atomically; dispatch disabled in P4 (§7) |
| `audit_event` | append-only, SHA-256 hash-chained (serialized by an advisory lock); merchant-scoped or security-scoped (NULL merchant, never shown to merchants) |
| `idempotency_record` | (merchant, operation key) → request hash + stored response; replay returns the stored response, a changed body returns `409` |
| `llm_budget`, `llm_spend_reservation` | PostgreSQL metering for multiple instances (§8) |

Not duplicated: cash balances. Capacity stays in P1 `capacity_evidence` and `capacity_commitment`; the journal and inbox are untouched. Dashboards read evidence and commitments; no cached balance table is added.

## 6. Workflows and state machines

**Offer (amendment proposal), backed by P1 revisions:**

```
proposed --counterparty accepts--> agreed        (writes AUTHORIZED revision, authorized_by = {debtor, creditor})
proposed --counterparty rejects--> rejected      (writes REJECTED revision by the rejecting party)
proposed --counterparty counters--> countered    (child offer = new DRAFT superseding it)
proposed --proposer withdraws--> withdrawn       (REJECTED revision by the proposer)
proposed --now >= expires_at--> expired          (derived; any decision after it -> 409 OFFER_EXPIRED)
proposed --in-force revision changed--> superseded (decision -> 409 STALE_REVISION)
```

- **Proposing.** Creating an offer records the proposer's own acceptance; this needs `offer:propose` + `offer:decide` and a recent step-up. It writes the DRAFT revision with `authorized_by = {proposer}`.
- **Accepting.** The other party accepts independently, from its own session and membership, with its own step-up. Its decision must carry the exact `terms_digest` it reviewed. The digest covers obligation, base revision, debtor, creditor, cash, concession, currency, due, expiry and offer id.
- **Rejected requests.**

  | Request | Response |
  |---|---|
  | Digest mismatch | `409 STALE_DIGEST` |
  | Second decision by the same merchant | `409 ALREADY_DECIDED` |
  | Decision by a non-party | `404` |
  | Proposer trying to accept on the counterparty's behalf | `403 NOT_COUNTERPARTY`; the proposer's merchant has already decided |

- **When the amendment counts.** It becomes authoritative only when the AUTHORIZED revision exists; P1 `InvoiceRecord.add_revision` re-checks that both parties authorized and that the accepted terms equal the offer.
- **Concurrency.** All transitions take `SELECT … FOR UPDATE` on the obligation row, then the offer row, in that order. Two concurrent accepts or counters therefore serialize, and exactly one wins.
- **Payment is separate.** An agreed amendment authorizes nobody to pay.

**Plan and payment authorization:**

```
plan_snapshot (immutable)  : computed for an initiator from the permissioned network at as_of, verified by the
                             independent P2 replay before storage
leg authorization          : pending -> approved | declined   (payer only, bound to plan digest, amount, payee,
                             fee ceiling, currency, expiry; step-up)
plan status (derived)      : verified -> awaiting_consent -> executable (all required approvals recorded)
                             | expired | superseded (inputs digest changed) | declined
payment_intent (P5)        : created only if: payer approval exists and is unexpired, the plan is not superseded
                             (inputs re-derived and digest equal), all amendment revisions it relies on are AUTHORIZED,
                             the membership is still active, capacity is reserved atomically
                             (P1 reserve_capacity), and the operation key is new or replays the same request
```

## 7. Payment execution boundary

- **No model, browser or optimizer authority over payments.** None of them can create or capture a PayPal payment. The planner writes snapshots only. The AI modules still cannot import `api/integrations/` (test enforced).
- **Intent creation.** `create_payment_intent` checks every precondition in §6 inside one transaction. It then writes `payment_intent` (`awaiting_payer_action`) and `outbox_message` (`held`) together. `PAYMENTS_EXECUTION_ENABLED` defaults to `false`, and no P4 worker dispatches anything. P5 adds the worker, which runs only after the owner authorizes the final Sandbox payment.
- **Webhooks.** The endpoint accepts only well-formed PayPal envelopes carrying transmission headers, and only when a webhook verifier is configured; otherwise it returns `503`. Events are deduplicated through the P1 `provider_event_inbox` by provider event id plus environment. They never change financial state by themselves: an accepted event enqueues a `provider_fetch` outbox message (held), because provider truth comes from a GET. The P4 verifier is an interface with a refusing default; the PayPal verify-signature call is P5.
- **Provider-confirmed vs spendable.** These remain separate states in every view (D006).

## 8. LLM metering for production

The P3 file ledger is a single-host development guard. P4 adds `PgSpendGuard` over `llm_budget` and `llm_spend_reservation`, a design that holds across any number of instances.

- **Reserve.** `SELECT … FOR UPDATE` on the budget row, then `committed + reserved + estimate ≤ cap`. It inserts a reservation with a server-generated UUID identity.
- **Commit and release are idempotent per reservation.** Repeating the same settlement returns the stored result. A different settlement of an already-settled reservation raises `ReservationConflictError`; an unknown id raises `UnknownReservationError`.
- **Overage is explicit.** If an actual usage-based cost exceeds the estimate and pushes committed spend over the cap, the commit still records the real cost (the provider has already charged it). The budget is then marked `exceeded`, with the overage amount stored, and every further reservation is refused until an operator records an explicit acknowledgement with a reason. The overage is never hidden or clipped.
- **Stale reservations.** These are reservations whose process died. They stay counted until an operator releases them, with a reason. They are never released silently.
- **Still an estimate.** This remains a local estimate over published prices, not provider billing; production must reconcile against the provider's usage export.

The file guard gains the same identity and idempotency rules. No new paid inference is authorized by P4: tests use scripted adapters, and the workspace narrates with deterministic templates unless the owner enables a model later.

## 9. Recommendations in the workspace

`GET /v1/m/{m}/opportunities` builds the permissioned network from PostgreSQL at `now`:
- capacity evidence and active commitments;
- confirmed policies;
- obligations and revisions between participating merchants.

It runs the P3 analyzer and returns `merchant_view` only, enriched with:
- **capacity basis.** `PROVIDER_VERIFIED`, `MERCHANT_ATTESTED`, `SYNTHETIC` or `NONE`/`EXPIRED`, with `observed_at` and `valid_until`;
- **gain basis.** The matched-baseline increment, separated from gross flow, for the initiator only;
- **approvals.** Recorded, pending and approved actions from the database, not assumptions;
- **policy status.** A merchant without a confirmed policy gets `POLICY_NOT_CONFIRMED`, and its "accept" is never assumed attractive.

Aggregate network figures go to the initiator only under two conditions; otherwise they are withheld with a reason code:
- every other contributing participant has `share_aggregates = true`;
- at least two other payers contribute (small-group suppression, doc 11).

`NO_ACTION` is returned whenever nothing improves on the matched baseline.

## 10. Threat scenarios → controls → tests

| Threat (doc 11) | Control | Test |
|---|---|---|
| Cross-tenant read of reserves, invoices, offers, plans, audit | membership check per request; object-level checks; identical 404 | negative authorization matrix per role and merchant |
| Approving for another merchant | decision merchant = path merchant with active membership; must be a party and the pending one | proposer cannot accept own offer for counterparty; outsider 404 |
| Stale or altered terms | `terms_digest` / plan digest binding; base-revision check | 409 on changed digest, superseded revision |
| Replay / duplicate decision | idempotency record + unique (offer, merchant) | same key → same response; changed body → 409; second decision → 409 |
| Expired offer | expiry check under row lock | decision at expiry → 409 |
| Approval race | row locks in a fixed order; partial unique open offer | concurrent accept/counter: exactly one succeeds |
| Concurrent payment plans on the same cash | P1 `reserve_capacity` locks the merchant; one open payment authorization per option | two intents over the same cash → one |
| Revoked access | membership checked on every request | revoked user gets 404 immediately, sessions remain but are useless |
| Session abuse / CSRF | HttpOnly SameSite cookie, CSRF header, Origin check, idle/absolute expiry, rotation | missing/incorrect CSRF → 403; expired session → 401; fixation impossible |
| Credential stuffing | DB rate limits, constant-work verify | 6th failure → 429 |
| Sensitive data in responses | merchant views only; no password hash, token or other merchant's private data serialized | response-scan test with distinctive private values |
| Prompt / data isolation | model prompts built only from merchant views (P3); workspace uses templates | privacy probe over workspace responses |
| Duplicate / malformed webhook | schema check, verifier required, inbox dedupe, no state change from body | malformed → 422, duplicate → one inbox row |
| Audit tampering | append-only trigger, hash chain | chain verification test, UPDATE refused |

## 11. Consequences

- New module `api/workflow/` (persistence and services for networks, offers, plans, decisions, audit and idempotency) and `api/app/auth/` (identity). The layout in CLAUDE.md §4 already allows auth under `app/`; `workflow/` is a new domain-service module inside the monolith.
- New dependency: `argon2-cffi`.
- The P3 demo routes remain loopback-only and unauthenticated *demo* endpoints over fixtures. The authenticated `/v1/m/...` routes are the product surface.
- **Not claimed:** legally binding electronic signatures. Decisions are authenticated, auditable application evidence only.

## 12. Implementation addendum (2026-11-11)

Implemented as designed above, with these decisions taken during implementation:

1. **CSRF token derivation.** The synchronizer token is `HMAC-SHA256(session token, "elide-csrf-v1")`, not a random value. Every tab of one session therefore gets the same token after a reload, and only its SHA-256 is stored. A cross-site page cannot compute it, because the session token is HttpOnly. This is the OWASP HMAC-based token pattern.
2. **Offers are signed by their proposer.** Creating an offer or counter records the proposer's own acceptance, so the proposer needs `offer:propose` + `offer:decide` and a recent step-up. The P1 DRAFT carries `authorized_by = {proposer}`. A withdrawal is a REJECTED revision by the proposer; no second decision row is written, because the proposer has already decided.
3. **The pay-by date is the amended due date.** An accepted early-payment offer becomes AUTHORIZED terms "pay `cash` by `pay_by`" (`expires_at ≤ pay_by ≤ current due date`; never later, so no credit is extended). The P2 planner then treats paying by that date as on time.
4. **Synthetic evidence lasts 7 days in the seed.** With 24-hour evidence, the agreed pay-by date fell outside the fundable window, and the planner, correctly, called a payment today "early". The seed now uses 7-day synthetic evidence (labelled). Real evidence keeps whatever validity its provenance supports.
5. **Re-planning.** `GET /opportunities` re-plans at request time, so plan ids change as `as_of` moves.
   - A payer's decision is shown, and enforced as the one decision, for identical live terms (same option = invoice revision, payer, payee, amount) across plans until it expires.
   - The intent path re-validates against current data regardless of which plan the decision came from.
   - Found in the browser check; a regression test was added.
6. **Invoices in execution.** An invoice with an active payment intent is not planned again: its reserved cash stays committed and dependent payments wait for provider confirmation and verified availability. Found in the browser check; a regression test was added.
7. **P3 contract test.** `test_no_confirmation_or_payment_endpoints_exist` now asserts the property it protected: the unauthenticated demo surface still has no confirm, approve, sign or pay route. It is joined by a sweep showing every `/v1/m/...` route answers `401` without a session.
8. **No model in the workspace.** The workspace makes no model calls. Narration uses the deterministic template, counteroffer help returns deterministic bounds, and policy drafting without fields reports `MODEL_UNAVAILABLE`. A workspace model must be explicitly attached (`app.state.workspace_llm`) and authorized by the owner.

**Not done in P4 (by design or deferred):**
- MFA and an OIDC identity provider. Session issuance is the seam for adding them.
- Membership granting through the UI; seed and scripts only.
- Settlement events persisted from provider receipts (P5).
- An outbox worker, the PayPal verify-signature call, and the payer-hosted session (P5, needing owner authorization for the final Sandbox payment).
- Playwright end-to-end tests. Browser workflow checks were done with Claude in Chrome (`evidence/phase4/browser_workflow_2026-11-11.md`). Component and workflow tests run in jsdom against a scripted backend; API workflows run over HTTP against PostgreSQL.
