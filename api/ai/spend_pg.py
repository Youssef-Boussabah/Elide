"""PostgreSQL LLM metering for multiple application instances (ADR-0011 §8).

Same protocol as the P3 file guard (`reserve` -> `commit` | `release`), with the budget
row as the serialization point (`SELECT ... FOR UPDATE`), so any number of processes on
any number of hosts share one cap. Amounts are integer micro-USD.

- Reservations have a server-generated UUID identity; settling an unknown id raises
  `UnknownReservationError`; repeating the same settlement is a no-op; settling it
  differently raises `ReservationConflictError`.
- If an actual usage-based cost pushes committed spend over the cap, the cost is still
  recorded (the provider already charged it), the overage is stored, and the budget is
  marked exceeded: every further reservation is refused until an operator records an
  acknowledgement with a reason. Nothing is clipped or hidden.
- Reservations of crashed processes stay counted until an operator releases them with
  a reason (`release_stale`); they are never freed silently.

Still a LOCAL estimate over published prices, not provider billing; production must
reconcile against the provider's usage export.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal

from sqlalchemy import Engine, insert, select, update

from api.ai.spend import (
    BudgetExceededError,
    Reservation,
    ReservationConflictError,
    SpendCapExceededError,
    UnknownReservationError,
)
from api.ai.spend_tables import llm_budget, llm_spend_reservation

MICRO = Decimal("1000000")


def to_micro(usd: Decimal) -> int:
    return int((Decimal(usd) * MICRO).to_integral_value(rounding=ROUND_UP))


def from_micro(m: int) -> Decimal:
    return Decimal(m) / MICRO


class PgSpendGuard:
    def __init__(self, engine: Engine, budget_id: str, cap_usd: Decimal | None = None) -> None:
        self.engine, self.budget_id = engine, budget_id
        if cap_usd is not None:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            with engine.begin() as conn:
                conn.execute(
                    pg_insert(llm_budget)
                    .values(budget_id=budget_id, cap_micro_usd=to_micro(cap_usd))
                    .on_conflict_do_nothing()
                )

    def _budget(self, conn):
        b = (
            conn.execute(
                select(llm_budget).where(llm_budget.c.budget_id == self.budget_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if b is None:
            raise SpendCapExceededError(f"no budget {self.budget_id!r}: no call may be made")
        return b

    def reserve(self, estimate: Decimal) -> Reservation:
        est = to_micro(estimate)
        if est <= 0:
            raise ValueError("a reservation must be positive")
        with self.engine.begin() as conn:
            b = self._budget(conn)
            if b["exceeded_at"] is not None and b["acknowledged_at"] is None:
                raise BudgetExceededError(
                    "committed spend exceeded the cap; an operator must acknowledge it"
                )
            if b["committed_micro_usd"] + b["reserved_micro_usd"] + est > b["cap_micro_usd"]:
                raise SpendCapExceededError(
                    f"spend cap USD {from_micro(b['cap_micro_usd'])} would be exceeded; "
                    "no call made"
                )
            rid = str(uuid.uuid4())
            conn.execute(
                insert(llm_spend_reservation).values(
                    reservation_id=rid,
                    budget_id=self.budget_id,
                    estimate_micro_usd=est,
                    status="reserved",
                    created_at=datetime.now(UTC),
                )
            )
            conn.execute(
                update(llm_budget)
                .where(llm_budget.c.budget_id == self.budget_id)
                .values(reserved_micro_usd=llm_budget.c.reserved_micro_usd + est)
            )
        return Reservation(rid, from_micro(est))

    def _locked_reservation(self, conn, rid: str):
        self._budget(conn)  # lock order: budget, then reservation
        r = (
            conn.execute(
                select(llm_spend_reservation)
                .where(
                    llm_spend_reservation.c.reservation_id == rid,
                    llm_spend_reservation.c.budget_id == self.budget_id,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if r is None:
            raise UnknownReservationError(f"unknown reservation {rid}")
        return r

    def commit(
        self,
        reservation: Reservation,
        *,
        cost: Decimal,
        model: str,
        prompt_version: str,
        tokens: tuple[int, int],
        note: str = "",
    ) -> Decimal:
        actual = to_micro(cost)
        with self.engine.begin() as conn:
            r = self._locked_reservation(conn, reservation.reservation_id)
            if r["status"] == "committed" and r["actual_micro_usd"] == actual:
                return from_micro(self._budget(conn)["committed_micro_usd"])
            if r["status"] != "reserved":
                raise ReservationConflictError(
                    f"reservation {reservation.reservation_id} already {r['status']}"
                )
            now = datetime.now(UTC)
            conn.execute(
                update(llm_spend_reservation)
                .where(llm_spend_reservation.c.reservation_id == reservation.reservation_id)
                .values(
                    status="committed",
                    actual_micro_usd=actual,
                    model=model[:120],
                    prompt_version=prompt_version[:40],
                    note=(note or f"tokens in={tokens[0]} out={tokens[1]}")[:500],
                    settled_at=now,
                )
            )
            b = self._budget(conn)
            committed = b["committed_micro_usd"] + actual
            values = {
                "committed_micro_usd": committed,
                "reserved_micro_usd": b["reserved_micro_usd"] - r["estimate_micro_usd"],
            }
            if committed > b["cap_micro_usd"]:
                values["overage_micro_usd"] = committed - b["cap_micro_usd"]
                if b["exceeded_at"] is None:
                    values["exceeded_at"] = now
            conn.execute(
                update(llm_budget).where(llm_budget.c.budget_id == self.budget_id).values(**values)
            )
            return from_micro(committed)

    def release(self, reservation: Reservation, *, reason: str = "not sent") -> None:
        with self.engine.begin() as conn:
            r = self._locked_reservation(conn, reservation.reservation_id)
            if r["status"] == "released":
                return
            if r["status"] != "reserved":
                raise ReservationConflictError(
                    f"reservation {reservation.reservation_id} already {r['status']}"
                )
            conn.execute(
                update(llm_spend_reservation)
                .where(llm_spend_reservation.c.reservation_id == reservation.reservation_id)
                .values(status="released", note=reason[:500], settled_at=datetime.now(UTC))
            )
            conn.execute(
                update(llm_budget)
                .where(llm_budget.c.budget_id == self.budget_id)
                .values(
                    reserved_micro_usd=llm_budget.c.reserved_micro_usd - r["estimate_micro_usd"]
                )
            )

    def release_stale(self, reservation_id: str, *, reason: str) -> None:
        """Operator action for a reservation whose process died (never automatic)."""
        if not reason.strip():
            raise ValueError("releasing a stale reservation needs a reason")
        self.release(Reservation(reservation_id, Decimal(0)), reason=f"operator: {reason}")

    def acknowledge_overage(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("an overage acknowledgement needs a reason")
        with self.engine.begin() as conn:
            self._budget(conn)
            conn.execute(
                update(llm_budget)
                .where(
                    llm_budget.c.budget_id == self.budget_id,
                    llm_budget.c.exceeded_at.isnot(None),
                )
                .values(acknowledged_at=datetime.now(UTC), acknowledged_reason=reason[:500])
            )

    def state(self) -> dict[str, object]:
        with self.engine.connect() as conn:
            b = (
                conn.execute(select(llm_budget).where(llm_budget.c.budget_id == self.budget_id))
                .mappings()
                .one()
            )
        return {
            "cap_usd": str(from_micro(b["cap_micro_usd"])),
            "committed_usd": str(from_micro(b["committed_micro_usd"])),
            "reserved_usd": str(from_micro(b["reserved_micro_usd"])),
            "overage_usd": str(from_micro(b["overage_micro_usd"])),
            "exceeded": b["exceeded_at"] is not None,
            "acknowledged": b["acknowledged_at"] is not None,
            "label": "LOCAL_ESTIMATE_NOT_PROVIDER_BILLING",
        }
