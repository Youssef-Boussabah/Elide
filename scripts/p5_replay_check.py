"""Exactly-once replay of the P5 pilot reconciliation. Provider: READ-ONLY.

    DATABASE_URL=<pilot database url> uv run python scripts/p5_replay_check.py \\
        --intent pin_7452d6099eaf9caf

After the payment was booked, this replays it four ways and proves no financial record is
added:
1. duplicate provider notifications: two `provider_fetch` jobs (the invoice and the capture)
   and a second `reconcile_payment` job, processed by the worker with provider-MUTATING
   execution disabled (only GETs are possible);
2. a direct second call of the booking routine with freshly fetched provider bodies;
3. re-posting the identical journal bundle (every entry must come back DUPLICATE);
4. the independent checker and the trial balance, before and after.

Local writes: the three replay jobs (outbox). PayPal calls: OAuth and GETs only. Writes
`evidence/phase5/replay_<UTC>.json`.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, text  # noqa: E402

from api.accounting.payer_postings import payer_entries  # noqa: E402
from api.accounting.reconciliation_check import CaptureExpectation, check_capture  # noqa: E402
from api.accounting.settlement_postings import settlement_entries  # noqa: E402
from api.accounting.store import post_entries  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.domain.money import USD, Money  # noqa: E402
from api.execution.runtime import build_runtime, worker_kinds  # noqa: E402
from api.execution.service import book, handlers_for, on_dead, schedule  # noqa: E402
from api.execution.tables import payment_session, provider_settlement  # noqa: E402
from api.integrations.paypal.client import PayPalSandboxClient  # noqa: E402
from api.integrations.paypal.execution import PayPalSandboxProvider  # noqa: E402
from api.integrations.paypal.redaction import fingerprint  # noqa: E402
from api.integrations.paypal.settlement import settlement_from_bodies  # noqa: E402
from api.workers.outbox import RetryPolicy, run_once  # noqa: E402
from api.workflow.store import load_record  # noqa: E402
from api.workflow.tables import payment_intent  # noqa: E402

TABLES = (
    "journal_entry",
    "journal_posting",
    "obligation_settlement_event",
    "provider_settlement",
    "capacity_commitment",
    "payment_exception",
)


def _counts(engine) -> dict:
    with engine.connect() as c:
        out = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}  # noqa: S608
        out["trial_balance"] = {
            cur: int(n)
            for cur, n in c.execute(
                text("SELECT currency, SUM(amount_cents) FROM journal_posting GROUP BY currency")
            ).all()
        }
        return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--intent", required=True)
    args = ap.parse_args(argv)
    s = get_settings()
    if s.payments_execution_enabled:
        print("REFUSED: run with PAYMENTS_EXECUTION_ENABLED=false (read-only provider access)")
        return 2
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    with engine.connect() as c:
        if c.execute(text("SELECT current_database()")).scalar_one() != "elide_sandbox_pilot":
            print("REFUSED: not the pilot database")
            return 2
        intent = dict(
            c.execute(select(payment_intent).where(payment_intent.c.intent_id == args.intent))
            .mappings()
            .one()
        )
        sess = dict(
            c.execute(select(payment_session).where(payment_session.c.intent_id == args.intent))
            .mappings()
            .one()
        )
        st = dict(
            c.execute(
                select(provider_settlement).where(provider_settlement.c.intent_id == args.intent)
            )
            .mappings()
            .one()
        )
        c.rollback()
    if intent["status"] != "reconciled":
        print(f"REFUSED: the intent is {intent['status']}, not reconciled")
        return 2
    before = _counts(engine)
    now = datetime.now(UTC)
    report: dict = {
        "run_id": now.strftime("%Y%m%dT%H%M%SZ"),
        "environment": "sandbox",
        "intent": args.intent,
        "capture_id": st["capture_id"],
        "before": before,
    }

    # 1. Duplicate notifications + another reconcile, worker with mutations disabled.
    with engine.begin() as c:
        for key, payload in (
            (
                "replay:fetch:invoice",
                {"invoice_id": sess["provider_invoice_id"], "provider_event_id": "REPLAY-INVOICE"},
            ),
            (
                "replay:fetch:capture",
                {"capture_id": st["capture_id"], "provider_event_id": "REPLAY-CAPTURE"},
            ),
        ):
            schedule(
                c, kind="provider_fetch", dedupe_key=key, intent_id=None, payload=payload, at=now
            )
        schedule(
            c,
            kind="reconcile_payment",
            dedupe_key=f"replay:reconcile:{args.intent}",
            intent_id=args.intent,
            payload={"intent_id": args.intent},
            at=now,
        )
    client = PayPalSandboxClient(s)
    provider = PayPalSandboxProvider(client)
    rt = build_runtime(
        engine, s, provider, worker_id="replay-check", execution_enabled=lambda: False
    )
    kinds = worker_kinds(False)
    done = run_once(
        engine,
        handlers_for(rt),
        worker_id=rt.worker_id,
        clock=rt.clock,
        policy=RetryPolicy(),
        on_dead=lambda j, e: on_dead(rt, j, e),
        kinds=kinds,
    )
    report["replay_1_jobs"] = [
        {"kind": j.kind, "action": r.action, "error": r.error} for j, r in done
    ]
    report["replay_1_kinds_claimable"] = kinds

    # 2. A direct second booking with fresh provider bodies.
    try:
        inv = client.get_invoice(sess["provider_invoice_id"]).body
        cap = client.get_capture(st["capture_id"]).body
    finally:
        client.close()
    res = book(rt, intent, sess, inv, cap, fingerprint((cap.get("payee") or {}).get("merchant_id")))
    report["replay_2_direct_booking"] = {"action": res.action, "error": res.error}

    # 3. Re-post the identical journal bundle.
    settlement = settlement_from_bodies(inv, cap, "replay:get_invoice", "replay:get_capture")
    with engine.connect() as c:
        issued = load_record(c, intent["obligation_id"]).record.obligation.issued_at
        c.rollback()
    entries = settlement_entries(settlement, intent["payee_merchant"]) + payer_entries(
        payer_entity=intent["payer_merchant"],
        obligation_id=intent["obligation_id"],
        capture_id=st["capture_id"],
        cash=Money(int(sess["expected_total_cents"]), USD),
        concession=Money(int(sess["expected_discount_cents"]), USD),
        recognized_at=issued,
        paid_at=settlement.ordering_time(),
    )
    posted = post_entries(engine, entries)
    report["replay_3_journal_bundle"] = [f"{p.idempotency_key}: {p.status.value}" for p in posted]

    # 4. Nothing added; the independent checker still agrees.
    after = _counts(engine)
    report["after"] = after
    with engine.connect() as c:
        report["independent_checker"] = check_capture(
            c,
            CaptureExpectation(
                st["capture_id"],
                st["invoice_id"],
                intent["payee_merchant"],
                intent["payer_merchant"],
                intent["obligation_id"],
                2500,
                2400,
                100,
            ),
        )
        c.rollback()
    ok = (
        before == after
        and all(p.status.value == "duplicate" for p in posted)
        and res.action == "done"
        and report["independent_checker"] == []
        and all(v == 0 for v in after["trial_balance"].values())
    )
    report["exactly_once"] = ok
    body = json.dumps(report, indent=2, sort_keys=True, default=str)
    body = body.replace(socket.gethostname() + ":", "redacted-host:")
    if "@" in body or "https://" in body:
        print("REFUSED: evidence would contain an email or URL")
        return 3
    path = ROOT / "evidence" / "phase5" / f"replay_{report['run_id']}.json"
    path.write_text(body + "\n", encoding="utf-8")
    print(body)
    print(
        f"{'EXACTLY ONCE: CONFIRMED' if ok else 'MISMATCH: REVIEW'}; wrote {path.relative_to(ROOT)}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
