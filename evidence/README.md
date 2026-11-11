# evidence/

Redacted, replayable proof for claims. Nothing here may contain access tokens, client
secrets, real emails, names, or hosted payer URLs.

- `paypal-sandbox/runs/<UTC>.json` — written by `scripts/paypal_sandbox_check.py`
  (already redacted; review before committing).
- `phase0/` — command outputs captured during bootstrap.
- `phase1/` — P1 review reproduction (Synthetic-tested).
- `phase2/` — planner benchmark from `scripts/run_planner_benchmarks.py` (Synthetic-tested;
  solve times are machine-specific); `*_hardening.*` is the post-review addendum (adds
  discharged metrics and constructed examples, reproduces the original results);
  `hardening_reproduction_*.txt` records the plan-tampering defects before the fix.
- `**/raw/` and `**/unredacted/` are git-ignored scratch locations.

Evidence class must be stated: Specified / Synthetic-tested / Sandbox-verified / Pilot-validated.
