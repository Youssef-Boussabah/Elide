# ADR-0013 — Agreed early-payment terms and explicit merchant payment decisions

- **Status:** **Accepted by the owner (2026-11-12) for implementation after** the controlled P5
  Sandbox payment evidence is captured and reviewed. Not implemented. Until it is, payment
  authority is unchanged. The strict xfail
  `tests/integration/test_p5_plan_blocker.py::test_min_early_discount_should_count_the_already_agreed_concession`
  stays a strict xfail and may turn into a pass only through the semantics below, implemented
  and tested in both the solver and the verifier.
- **Context:** the P5 pilot plan was empty after the buyer accepted a USD 1.00 early-payment
  offer (`evidence/phase5/pilot_plan_blocker_2026-11-12.md`). The decisive cause was
  merchant-attested evidence (24 h) shorter than the payment window (about 68 h); the owner's
  R1 renews it for 72 h. The investigation also exposed four real modelling issues:
  1. the early-discount rule is measured against the wrong baseline;
  2. a slot's start instant stands for a payment the payer may make at any time up to the
     deadline;
  3. the product cannot express a merchant's explicit "pay this now" decision;
  4. evidence validity is not checked over the whole payment window.
- **Sources:** ADR-0008 (revisions, settlement identity), ADR-0009 (solver, verifier,
  baselines), ADR-0011 (authorizations, conditions), ADR-0012 (execution, revalidation), doc 05
  (optimization), doc 12 (baselines and proof).

## 1. Original terms versus agreed amended terms

| Term set | Source | Meaning |
|---|---|---|
| **Original terms** | revision 1 (AUTHORIZED original issue) | face, original due date `D0`, concession 0. The economic baseline B0 and the reference for the merchant's "early" policy |
| **Agreed terms** | the AUTHORIZED revision in force (signed by both parties) | open amount, total concession `C_agreed`, agreed pay-by `D_agreed ≤ D0` |
| **Option terms** | a plan option or a merchant decision | the cash and total concession that would discharge the invoice; an open offer or proposal may differ from the agreed terms |

`ObligationEdge` gains `original_due_at` and `original_concession` (always 0 in this
project) from revision 1, next to the in-force `due_at` and `in_force_concession`. Both are
already persisted; nothing new is stored for agreed terms.

## 2. Total agreed discount versus incremental discount

- `total_concession(o) = o.concession` (relative to the original terms).
- `incremental_concession(o) = o.concession − in_force_concession` (relative to the agreed
  terms; refused if negative, as today).

**Rules.**
- **Policy constraint (merchant rule "pay early only for at least M off"):** if the payment
  instant is before `D0`, then `total_concession ≥ M`. Paying before the *original* due date
  is what the rule governs, and an agreed concession counts towards it.
- **Acceleration test:** if the payment instant is before `D_agreed`, then
  `incremental_concession − time_cost(now → D_agreed) ≥ min_net_benefit`. Paying earlier than
  already agreed must pay for itself, measured only against what is not yet agreed.
- **Objective and reported benefit:** incremental only. An agreed concession is recognised
  once, when it is agreed (B5 vs B0 comparisons, doc 12), and never again as a plan gain.

**Worked pilot example.** Face 2500, `D0` = 11-22, agreed concession 100 with `D_agreed` =
11-15, policy M = 100, hurdle 800 bps.
- Paying on 11-14: policy met (100 ≥ 100); no acceleration beyond the agreed date; allowed,
  and the plan gain is 0.
- Paying on 11-12: policy met; acceleration test `0 − 2 < 0`, so refused for the optimizer;
  available only as an explicit merchant decision (§5) with that cost disclosed.

## 3. Solver and verifier consistency

- The rules of §2 and §4 are implemented **twice, independently**: in the solver's leg
  evaluation and in the replay verifier, which shares no evaluation code with it (ADR-0009).
- Parity tests run both on generated networks that include negotiated revisions. The
  canonical benchmark networks have none, so their results must stay identical; the re-run
  must show it.
- A verifier rejection always wins (D005).

## 4. Exact deadlines versus daily scheduling slots

**Today:**
- A slot `t` stands for one payment instant `start(t)`.
- "On time" is `start(t) ≤ due_at`.
- Evidence counts if `start(t) < valid_until`.
- Offers count if `start(t) < expires_at`.

That is consistent for *planning*, but a hosted payment happens at an instant **the payer
chooses** in `[sent, D_agreed]`.

**Decision:**
- **Planning** keeps daily slots: the slot start is the planned instant.
- **Validity for execution** uses exact instants, not slots. An intent's *payment window*
  is `[dispatch, deadline]`, where the deadline is `D_agreed` for an on-time leg, the planned
  slot start for an accelerated leg, or the instruction's window end (§5).
- Evidence must satisfy `observed_at ≤ dispatch` and `valid_until > deadline`, both at intent
  creation and at every dispatch revalidation. That closes the gap where a slot-start check
  passes but evidence lapses before the payer can still pay.
- Offer expiry stays exact (`accept < expires_at`).
- Due-date comparisons use the stored UTC instants. The business-timezone date is used only
  for display and for PayPal's `due_date` field, recorded in the session (as today).

## 5. Explicit merchant-directed payment decision

A **payment instruction** is the payer's own, explicit decision to pay an agreed obligation
inside a stated window. It is never presented as, or mixed with, the optimizer's
recommendation. The optimizer may still say NO_ACTION, and both are shown side by side.

- **Record.** Append-only `payment_instruction` with these fields:
  - payer merchant, obligation, in-force revision and its content hash;
  - amount = remaining open, payee, currency (USD), fee ceiling;
  - window `[created_at, window_end ≤ D_agreed]`;
  - conditions and conditions digest (ADR-0011 rules);
  - economics disclosure (§5.2) and its digest;
  - user, session and expiry (24 h, like approvals).

  Unique per open instruction and obligation.
- **Who.** The payer merchant only, with `payment:authorize` and a recent step-up. A
  counterparty, the optimizer, the model or the worker can never create one.
- **Economics disclosure (shown before confirmation; the confirmation carries its digest):**
  - against the original terms: the agreed concession earned and the days before `D0`;
  - against the agreed terms: incremental benefit (0 for an agreed obligation) and the time
    cost from now to `D_agreed` at the merchant's stated hurdle;
  - the optimizer's current recommendation for the same obligation, verbatim.
- **Execution.** The same P5 payment intent, reservation, outbox, revalidation, worker,
  reconciliation and booking as a plan leg. `verify_leg_now` is replaced by the instruction
  check for instruction-backed intents.

## 6. What an instruction must satisfy, at creation and at every dispatch

All of these are checked by an independent function, not by the optimizer:

1. **Terms:**
   - the in-force revision is AUTHORIZED by both parties and unchanged (content hash);
   - not disputed; USD; amount = remaining open.
2. **Consent:** the instruction is the payer's; its conditions digest equals the recomputed
   one (policy version, effective reserve, fee terms, payee, participation, evidence basis);
   it is unexpired; the instructing user still holds `payment:authorize` (share-locked).
3. **Policy:**
   - the original-terms discount rule (§2);
   - the payee is not prohibited;
   - the payer fee is at most the policy's per-leg fee cap and at most the fee ceiling.
4. **Cash:**
   - own verified capacity only: no reliance on incoming or provider-confirmed funds
     (D006/D008);
   - after amount plus fee ceiling, `deployable ≥ 0` with the reserve at
     max(evidence reserve, confirmed policy floor);
   - all other active commitments are counted.
5. **Evidence window:** valid through `window_end` (§4); synthetic evidence is never allowed
   outside synthetic networks (as P4).
6. **Provider:** both bindings active; the credential fingerprint matches the invoicer
   binding; Sandbox only; the kill switch is on.
7. **No dependency:** no upstream leg; an instruction never funds itself from a receipt.

Any failure at dispatch cancels the intent before anything payable exists, or goes to review
if an invoice exists, exactly as in ADR-0012 §5.

## 7. Regression benchmarks and independent financial verification

- **Characterization first:** the five tests of `test_p5_plan_blocker.py`; the strict xfail
  turns into a pass only once §2 is implemented in both solver and verifier.
- **Solver and verifier parity** on networks with negotiated revisions, original vs agreed due
  dates, policies with and without M, hurdles, and evidence lapsing inside, at the end of and
  after the payment window.
- **Brute-force equivalence** (ADR-0009) re-run on its seeded corpus extended with negotiated
  revisions. B0–B5 benchmark re-run with seeds fixed; canonical results unchanged, or every
  difference explained.
- **Instructions:**
  - creation and refusal for every condition in §6;
  - a reserve or policy change, a revocation, a revision change and evidence lapse between
    creation and dispatch;
  - payee isolation;
  - labels in API and UI;
  - a Playwright flow against the mock provider.
- **Accounting:** a booking from an instruction-backed intent passes the independent
  reconciliation checker (ADR-0012 §7.3) and the settlement identity, exactly as a plan leg.

## Alternatives considered

- **Change `extra` to the total concession everywhere:** rejected. It double counts agreed
  terms and lets the optimizer "gain" what is already agreed.
- **Force a leg into the plan or skip the verifier:** rejected (D005).
- **Accept a short attestation for a longer payment window:** rejected. The reservation spans
  the window.
- **Make instructions a plan option:** rejected. It would present a voluntary decision as a
  recommendation and let it influence other legs' optimization.

## Finance and privacy impact

- No counterparty disclosure changes; the disclosure stays private to the payer.
- Voluntary acceleration is explicit and its cost is shown.
- Reserves, policy floors, fee caps and D006/D008 are unchanged.
- The settlement identity and the append-only journal are unchanged.
