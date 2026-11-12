# P5 gate review: reproduction of the two findings (2026-11-12)

Owner review of `613d72fc92f1d0614d86db1b6c02120708b6bad0` (CI run #25). Both findings were
reproduced on that commit **before any fix**, with two behavioural tests
(`tests/integration/test_p5_gate_review.py`, the first two cases). The only change in the
tree at that moment was test instrumentation in the mock provider (it records the
`PayPal-Request-Id` of every mutating call). Mock provider only; no PayPal call.

```
$ uv run pytest tests/integration/test_p5_gate_review.py      # on 613d72f + mock logging
E  AssertionError: assert [('cancel_inv...01-B433D6',))] == []
E    Left contains one more item: ('cancel_invoice', ('INV2-MOCK-0001-B433D6',))
E  AssertionError: assert 'op-d004d78270599278f8d3d578' == 'op-3ebef0f727ba5c20a11fdb6f'
FAILED ...::test_deadline_passing_never_cancels_when_auto_cancel_is_disabled
FAILED ...::test_a_retried_create_reuses_its_original_request_id
2 failed
```

## Finding 1: automatic cancellation at the pay deadline

- **Reproduced:** advancing the clock four days past the agreed pay-by time made the worker
  call `POST /v2/invoicing/invoices/{id}/cancel` (mock), then release the cash.
- **Root cause:** `observe()` called `request_cancel(..., EXPIRED)` unconditionally once
  `now >= pay_deadline`. The revocation-after-send path (`AUTHORIZATION_REVOKED`) was a
  second automatic cancellation. There was no policy switch.
- **Fix:** `AUTO_CANCEL_UNPAID` (default `false`; `Runtime.auto_cancel`). Disabled: the
  deadline or a revoked authority opens an operator-review exception
  (`PAY_DEADLINE_PASSED_REVIEW` / `AUTHORIZATION_REVOKED_REVIEW`) once; no cancel call, no
  cash release, no local state change; read-only reconciliation continues (slower poll), so
  a late payment is still booked exactly once; only a provider-confirmed cancellation ends
  the payment; the payer link is refused after the deadline (`PAY_DEADLINE_PASSED`) and while
  a review is open. Explicit payer cancellation is unchanged. With the policy enabled, the
  previous behaviour is kept and still tested.

## Finding 2: a fresh PayPal-Request-Id on every attempt

- **Reproduced:** a create attempt that never reached the provider was retried with a
  different request id (`op-3ebef…` then `op-d004d…`).
- **Root cause:** `_begin_op()` generated `op-<random>` per call and used that attempt key
  as the `PayPal-Request-Id`. No logical request identity was persisted.
- **Fix:** migration `0007`: append-only `provider_request` (one row per intent and logical
  create/send/cancel, deterministic id `elide-<intent>-<op>`, digest of the request first
  sent). Every attempt links to it (`provider_operation.request_id`, `attempt_no`). A changed
  payload is refused (`PROVIDER_REQUEST_CONFLICT`, operator review, cash kept). A refusal of
  a *retried* mutation, or any 409, is treated as an unknown outcome and looked up. A
  mutation whose logical request already succeeded is never re-issued.

## Found while testing the fixes

- **Duplicate dispatch could send twice.** With two dispatch messages for one intent, a
  worker that read the DRAFT just before another worker's send succeeded issued a second
  send (same request id, so a provider honouring idempotency would have replayed it, but
  correctness must not depend on that). Fixed by the "already succeeded" guard above.
- **A duplicate dispatch raised an uncaught `TransitionError`** (approved -> session_pending
  taken by the other worker). It was retried by the worker's generic guard; it is now
  handled explicitly as `ALREADY_DISPATCHED`.
- **Late replies after takeover** changed nothing even before (the stale call had been
  reclassified), but post-call writes were not lease-fenced; they now are, and the late
  reply is recorded in the audit trail (`provider.late_reply`).
- **P3 spend-guard lock, Windows only (pre-existing, not P5 code).** The full suite failed
  once in `tests/unit/ai/test_spend_guard.py::test_separate_processes_share_the_cap`.
  Re-run in isolation it failed 1 in 5 and 1 in 19 runs with
  `PermissionError: [Errno 13] ... shared.json.lock` from `os.open(O_CREAT | O_EXCL)`.
  Root cause: on Windows, creating a lock file that another process is deleting ("delete
  pending") raises `PermissionError`, not `FileExistsError`, and the lock loop retried only
  the latter. It failed closed (no reservation, no model call), so there was no spend risk,
  but the guard was unreliable under contention. CI (Linux) has no delete-pending state.
  Fixed by retrying both errors until the lock timeout (a lasting permission problem
  still fails closed). Two deterministic regression tests were added; afterwards the
  multi-process test passed 30 of 30 runs. No model call was made.
