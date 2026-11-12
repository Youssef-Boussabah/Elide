"""Durable outbox dispatcher (ADR-0012 §4.3). PostgreSQL only: no broker.

- **Claim** (`claim`): one short transaction selects due messages with
  `FOR UPDATE SKIP LOCKED` (concurrent workers never claim the same row), including
  `leased` rows whose lease expired (a crashed worker), and leases them: owner, expiry,
  `lease_token + 1` (fencing token), `attempts + 1`. Committed before any work.
- **Fencing** (`assert_lease`): before every provider-mutating call the handler re-checks,
  inside the transaction that records the call, that it still holds this exact lease. A
  worker whose lease was taken over stops without calling the provider.
- **Finish** (`finish`): compare-and-set on (id, owner, token). A worker that lost its lease
  cannot overwrite the new owner's result.
- **Retry policy**: exponential backoff with a cap and deterministic jitter; after
  `max_attempts` claims the message is dead-lettered (`dead`) and an `on_dead` hook lets
  the domain put the payment into operator review. Re-armed polling messages (`rearm`)
  reset the attempt counter: polling is not failure.
- No database lock is held while a handler talks to the provider.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Connection, Engine, and_, or_, select, update

from api.workflow.tables import outbox_message

log = logging.getLogger("elide.worker")

Action = Literal["done", "retry", "rearm", "failed", "dead"]


class LeaseLostError(RuntimeError):
    """This worker no longer holds the message's lease (taken over after expiry)."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    intent_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_token: int


@dataclass(frozen=True, slots=True)
class Result:
    action: Action
    delay_s: float = 0.0
    error: str | None = None


DONE = Result("done")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_s: float = 30.0
    cap_s: float = 1800.0

    def delay(self, attempt: int, msg_id: int) -> float:
        raw = min(self.base_s * (2 ** max(attempt - 1, 0)), self.cap_s)
        jitter = (msg_id * 2654435761 % 1000) / 1000 * (self.base_s / 2)
        return min(raw + jitter, self.cap_s)


def claim(
    engine: Engine,
    *,
    worker_id: str,
    now: datetime,
    kinds: Iterable[str],
    lease_s: float,
    limit: int = 10,
) -> list[Job]:
    m = outbox_message.c
    due = or_(
        and_(m.status == "pending", m.available_at <= now),
        and_(m.status == "leased", m.lease_until < now),
    )
    with engine.begin() as conn:
        ids = list(
            conn.execute(
                select(m.id)
                .where(due, m.kind.in_(list(kinds)))
                .order_by(m.available_at, m.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        if not ids:
            return []
        rows = conn.execute(
            update(outbox_message)
            .where(m.id.in_(ids))
            .values(
                status="leased",
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=lease_s),
                lease_token=m.lease_token + 1,
                attempts=m.attempts + 1,
                last_attempt_at=now,
            )
            .returning(
                m.id,
                m.kind,
                m.intent_id,
                m.payload,
                m.attempts,
                m.max_attempts,
                m.lease_owner,
                m.lease_token,
            )
        ).all()
    jobs = [Job(*r) for r in rows]
    return sorted(jobs, key=lambda j: j.id)


def assert_lease(conn: Connection, job: Job, now: datetime) -> None:
    """Inside the caller's transaction: hold the row and prove the lease is still ours."""
    m = outbox_message.c
    row = conn.execute(
        select(m.id)
        .where(
            m.id == job.id,
            m.status == "leased",
            m.lease_owner == job.lease_owner,
            m.lease_token == job.lease_token,
            m.lease_until > now,
        )
        .with_for_update()
    ).first()
    if row is None:
        raise LeaseLostError(f"lease on outbox message {job.id} lost")


def finish(engine: Engine, job: Job, result: Result, now: datetime, policy: RetryPolicy) -> bool:
    m = outbox_message.c
    values: dict[str, Any] = {"lease_owner": None, "lease_until": None, "last_error": None}
    if result.error:
        values["last_error"] = result.error[:200]
    if result.action == "done" or result.action == "failed":
        values.update(status=result.action, finished_at=now)
    elif result.action == "dead":
        values.update(status="dead", finished_at=now)
    elif result.action == "rearm":
        values.update(
            status="pending",
            attempts=0,
            available_at=now + timedelta(seconds=result.delay_s),
        )
    else:  # retry
        delay = result.delay_s or policy.delay(job.attempts, job.id)
        values.update(status="pending", available_at=now + timedelta(seconds=delay))
    with engine.begin() as conn:
        res = conn.execute(
            update(outbox_message)
            .where(
                m.id == job.id,
                m.status == "leased",
                m.lease_owner == job.lease_owner,
                m.lease_token == job.lease_token,
            )
            .values(**values)
        )
        return res.rowcount == 1


Handler = Callable[[Job], Result]


def run_once(
    engine: Engine,
    handlers: dict[str, Handler],
    *,
    worker_id: str,
    clock: Callable[[], datetime],
    lease_s: float = 120.0,
    limit: int = 10,
    policy: RetryPolicy | None = None,
    on_dead: Callable[[Job, str], None] | None = None,
    kinds: Iterable[str] | None = None,
) -> list[tuple[Job, Result]]:
    """Claim due work and run it. Returns what happened to each claimed message."""
    policy = policy or RetryPolicy()
    out: list[tuple[Job, Result]] = []
    for job in claim(
        engine,
        worker_id=worker_id,
        now=clock(),
        kinds=list(kinds if kinds is not None else handlers),
        lease_s=lease_s,
        limit=limit,
    ):
        if job.attempts > job.max_attempts:
            result = Result("dead", error=f"MAX_ATTEMPTS_EXCEEDED ({job.max_attempts})")
            if on_dead is not None:
                on_dead(job, result.error or "")
        else:
            try:
                result = handlers[job.kind](job)
            except LeaseLostError:
                log.warning("outbox %s: lease lost; stopping without further calls", job.id)
                out.append((job, Result("retry", error="LEASE_LOST")))
                continue
            except Exception as exc:
                log.exception("outbox %s (%s) handler error", job.id, job.kind)
                result = Result("retry", error=f"HANDLER_ERROR:{type(exc).__name__}")
            if result.action == "retry" and job.attempts >= job.max_attempts:
                result = Result("dead", error=f"RETRIES_EXHAUSTED: {result.error}")
                if on_dead is not None:
                    on_dead(job, result.error or "")
        finish(engine, job, result, clock(), policy)
        out.append((job, result))
    return out
