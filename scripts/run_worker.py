"""Run the P5 outbox worker (ADR-0012 §4.3).

    uv run python scripts/run_worker.py --once          # one pass, then exit
    uv run python scripts/run_worker.py --interval 10   # loop until Ctrl+C

Uses PAYMENT_PROVIDER / PAYMENTS_EXECUTION_ENABLED from the environment or `.env`. With
the kill switch off, only read-only work runs (reconciliation and webhook fetches); no
invoice is created, sent or cancelled. Safe to run several copies: messages are claimed
with SKIP LOCKED leases. Prints counts and codes only; never provider bodies or links.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402

from api.config import get_settings  # noqa: E402
from api.execution.runtime import build_provider, build_runtime, worker_kinds  # noqa: E402
from api.execution.service import handlers_for, on_dead  # noqa: E402
from api.workers.outbox import RetryPolicy, run_once  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=10.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = get_settings()
    provider = build_provider(settings)
    if provider is None:
        print("BLOCKED: PAYMENT_PROVIDER=none; nothing to dispatch")
        return 2
    engine = create_engine(settings.database_url, connect_args={"connect_timeout": 5})
    rt = build_runtime(engine, settings, provider)
    print(
        f"worker {rt.worker_id}: provider={settings.payment_provider} "
        f"execution_enabled={settings.payments_execution_enabled}"
    )
    while True:
        done = run_once(
            engine,
            handlers_for(rt),
            worker_id=rt.worker_id,
            clock=lambda: datetime.now(UTC),
            lease_s=float(settings.worker_lease_seconds),
            policy=RetryPolicy(),
            on_dead=lambda job, err: on_dead(rt, job, err),
            kinds=worker_kinds(settings.payments_execution_enabled),
        )
        for job, res in done:
            print(
                f"  message {job.id} {job.kind} intent={job.intent_id}: {res.action} {res.error or ''}"
            )
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
