# P5 pilot: held intent and false "CONDITIONS CHANGED" (2026-11-12)

**Status:** both issues reproduced; root causes established; fixes proposed, **not
implemented**. No payment authority changed, no PayPal call, no worker run, no mutation
enabled, and the pilot database was not altered: the diagnosis ran in a rolled-back
transaction.

- **Diagnosis:** `scripts/diagnose_pilot_intent.py`, output
  `evidence/phase5/pilot_intent_diagnosis_20261112T171911Z.json` (17:19Z).
- **Tests:** `tests/integration/test_p5_intent_release.py`.

## Observed records (isolated pilot database)

| Record | Value |
|---|---|
| Authorization `pau_8d756a08712328c3` | approve, revision 3, USD 24.00, fee ceiling 0, decided 17:13:07Z, **expires 2026-11-13T17:11:29Z**, conditions digest `f3205317…` |
| Intent `pin_7452d6099eaf9caf` | `approved`, reason `EXECUTION_DISABLED`, merchant-attested, created 17:13:24Z |
| Commitment `pi:pin_7452d6099eaf9caf` | `reservation`, USD 24.00, active (the only commitment) |
| Outbox | one message: `create_payment_session`, **held**, 0 attempts (no other message of any kind) |
| In-force revision | 3 (AUTHORIZED by both): USD 1.00 concession, open USD 24.00, pay-by 2026-11-15T13:13:32Z |
| Policy | version 1, reserve floor USD 50.00 |
| Evidence | merchant-attested USD 200.00 / USD 50.00, observed 17:10:32Z, **valid until 2026-11-15T17:10:32Z (covers the pay-by)** |
| Bindings | invoicer matches the configured credentials; payer matches the configured Sandbox payer |

## Issue 1: "CONDITIONS CHANGED: RE-APPROVE" and "plan stale" (false positive)

**Root cause:**
- `durable_plan_view` (`api/workflow/plans.py`) recomputes each leg's conditions with
  `load_network(...)`.
- `load_network` deliberately **excludes invoices that have an active payment intent**, so
  they are not planned twice.
- So once the intent existed, `pilot_001` was absent from the view's network:
  `payment_conditions` returned `None`, `authorization_status` reported `needs_reapproval`,
  and the economic digest differed, giving "plan stale".

The view never used the execution-specific load (`executing_intent=...`).

**The real conditions are unchanged.** Recomputed the way execution revalidates them (the
intent's own invoice included, its own reservation excluded):
- the conditions digest **equals** the authorization's;
- the independent verifier reports **no issues**;
- capacity after the reservation: 200.00 − 50.00 − 24.00 = USD 126.00 deployable.

So the UI warning is wrong. It is also not harmless: it invites a second approval for an
invoice that is already in execution. That would be refused, because one intent per invoice
is enforced, but it misleads the payer.

**Recommended fix (view only; no authority change):**
- When a leg's invoice has a non-terminal intent from this payer's authorization, report
  `{"status": "in_execution", "intent_id", "intent_status", "authorization_id"}` and offer no
  `authorize` block.
- Recompute that leg's conditions with `load_network(executing_intent=...)`. If they truly
  differ, report `in_execution_conditions_changed`: the worker would refuse at dispatch, and
  the payer should see that now.
- `plan_current` must ignore invoices that left the network only because they are in
  execution.
- UI: an "IN EXECUTION" label linking to the intent row.

## Issue 2: the held message is never released

**Root cause:**
- By design (ADR-0012 §4.3), an intent created while `PAYMENTS_EXECUTION_ENABLED=false` gets
  a **held** outbox message with `status_reason=EXECUTION_DISABLED`.
- Nothing ever moves a held message to pending: enabling execution later only affects intents
  created afterwards.
- That property is safe and is pinned by a passing test: enabling execution never dispatches
  a held intent by itself.
- What is missing is an explicit, verified way to release it. The pilot API was running with
  execution off (from the investigation) when the intent was prepared.

**Recommended mechanism:**
`POST /v1/m/{m}/payment-intents/{id}/release`, the payer only, with `payment:authorize`, a
recent step-up and an `Idempotency-Key`. It is the payer's explicit confirmation to dispatch
the *same* approved intent.

In **one transaction**:
1. Lock the intent row and its outbox row (`FOR UPDATE`).
2. Require:
   - the intent is `approved` and not synthetic;
   - `status_reason` is `EXECUTION_DISABLED` or `PROVIDER_NOT_CONNECTED`;
   - exactly one `create_payment_session` message for it, `held`, 0 attempts;
   - execution enabled now.
3. Run the **same `revalidate()` the worker runs before any provider mutation**:
   - the authorization is unexpired and is an approval;
   - the approver's membership is share-locked and still authorized;
   - the conditions digest, recomputed with `executing_intent`, matches;
   - the independent verifier is clean;
   - the commitment is active with exactly amount plus fee ceiling;
   - both bindings are active and match the credential and payer fingerprints (computed
     locally, no network);
   - Sandbox only.
4. Require no conflict:
   - no other non-terminal intent for the obligation;
   - no other pending or leased mutating job (create or cancel) for this intent;
   - no session or provider request recorded yet.
5. Set the outbox `held → pending` (`available_at = now`). Set the intent's `status_reason`
   to `QUEUED_FOR_PROVIDER`; the status stays `approved`. Write a `payment_state_event` with
   reason `RELEASED_BY_PAYER` and an audit event.
6. **Reuse** the original intent, authorization and commitment. Nothing is re-reserved or
   recreated.
7. **Idempotent:** the same key replays the stored response; a second release of an
   already-queued intent returns its current state without change. The database trigger
   still keeps synthetic messages held.

Any failure leaves everything unchanged and returns its code (for example
`AUTHORIZATION_EXPIRED`, `CONDITIONS_CHANGED`, `VERIFIER_REJECTED`, `EXECUTION_DISABLED`,
`CONFLICTING_JOB`). The worker revalidates again before the provider call.

UI: on a held intent, an **"Allow PayPal dispatch"** action (step-up, confirmation dialog),
shown only to the payer and only when execution is enabled.

**Timing for this intent:** the approval expires **2026-11-13T17:11:29Z**. Release and
dispatch must happen before then, or the payer approves again (a new decision, and the old
intent is cancelled). The evidence lasts until 2026-11-15T17:10Z.

## Tests

`tests/integration/test_p5_intent_release.py`:

| Test | Kind | State |
|---|---|---|
| Material conditions unchanged when computed as execution does | characterization | passes |
| Enabling execution never releases a held message | safety | passes |
| Reproduction: the view reports `needs_reapproval` and a stale plan | reproduction | passes |
| The view reports `in_execution` and offers no re-approval | specification | strict xfail (fails on `needs_reapproval`) |
| Release queues the same intent once, after revalidation; the worker then sends once | specification | strict xfail (route missing) |
| Release refused while execution is off | specification | strict xfail (route missing) |
