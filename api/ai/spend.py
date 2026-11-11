"""Software spend cap for real model calls (owner-authorized budget: USD 10.00 for P3).

Atomic reservation: before a call is sent, `reserve(estimate)` adds an upper-bound
estimate (generous input-token estimate + the full `max_tokens`, at the listed prices x a
safety factor) to the ledger *inside one critical section* and refuses if
`committed + outstanding reservations + estimate` would pass the cap. After the call,
`commit(reservation, actual)` replaces the reservation with the cost recomputed from the
provider's reported usage, or `release(reservation)` frees it when the request certainly
did not reach the provider. A call whose outcome is unknown (timeout, dropped
connection) is committed at its full estimate.

The critical section holds a per-process `threading.Lock` AND an exclusive lock file next
to the ledger (`os.O_CREAT | os.O_EXCL`), so threads and separate processes sharing the
same ledger cannot both reserve the last part of the budget. A lock that cannot be taken
within `lock_timeout` makes the call fail closed (no request is sent). A lock file left by
a crashed process must be removed by hand; the guard never breaks a lock on its own.

This is a LOCAL software cap over ESTIMATES. It is not a provider-side billing limit; the
provider console is the authoritative bill.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path

from api.ai.adapter import LLMUnavailableError

DEFAULT_CAP_USD = Decimal("10.00")
SAFETY_FACTOR = Decimal("1.5")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / ".elide" / "llm_spend.json"
_THREAD_LOCK = threading.Lock()


class SpendCapExceededError(LLMUnavailableError):
    pass


class SpendLockError(LLMUnavailableError):
    pass


@dataclass(frozen=True, slots=True)
class Prices:
    """USD per one million tokens."""

    input_per_m: Decimal
    output_per_m: Decimal
    source: str

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        raw = (
            Decimal(input_tokens) * self.input_per_m + Decimal(output_tokens) * self.output_per_m
        ) / Decimal(1_000_000)
        return (raw * SAFETY_FACTOR).quantize(Decimal("0.000001"), rounding=ROUND_UP)


def estimate_tokens(text: str) -> int:
    """Deliberately high: ~3 characters per token, plus framing overhead."""
    return len(text) // 3 + 64


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    amount: Decimal


class SpendGuard:
    def __init__(
        self,
        cap_usd: Decimal = DEFAULT_CAP_USD,
        ledger: Path = DEFAULT_LEDGER,
        lock_timeout: float = 10.0,
    ) -> None:
        self.cap = Decimal(cap_usd)
        self.ledger = Path(ledger)
        self.lock_path = self.ledger.with_suffix(self.ledger.suffix + ".lock")
        self.lock_timeout = lock_timeout

    # -- critical section -------------------------------------------------------------
    @contextmanager
    def _locked(self):
        with _THREAD_LOCK:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    if time.monotonic() > deadline:
                        raise SpendLockError(
                            f"spend ledger lock {self.lock_path.name} is held; no call made "
                            "(remove it by hand only if no other process is running)"
                        ) from None
                    time.sleep(0.005)
            try:
                os.write(fd, str(os.getpid()).encode())
                yield
            finally:
                os.close(fd)
                os.unlink(self.lock_path)

    def _load(self) -> dict:
        if not self.ledger.exists():
            return {"total_usd": "0", "reserved": {}, "calls": []}
        data = json.loads(self.ledger.read_text("utf-8"))
        data.setdefault("reserved", {})
        return data

    def _save(self, data: dict) -> None:
        tmp = self.ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), "utf-8")
        os.replace(tmp, self.ledger)

    # -- queries ----------------------------------------------------------------------
    def total(self) -> Decimal:
        """Committed estimated spend (excludes outstanding reservations)."""
        return Decimal(self._load()["total_usd"])

    def outstanding(self) -> Decimal:
        return sum((Decimal(v) for v in self._load()["reserved"].values()), Decimal(0))

    # -- reservation protocol -----------------------------------------------------------
    def reserve(self, estimate: Decimal) -> Reservation:
        estimate = Decimal(estimate)
        if estimate <= 0:
            raise ValueError("a reservation must be positive")
        with self._locked():
            data = self._load()
            committed = Decimal(data["total_usd"])
            held = sum((Decimal(v) for v in data["reserved"].values()), Decimal(0))
            if committed + held + estimate > self.cap:
                raise SpendCapExceededError(
                    f"spend cap USD {self.cap} would be exceeded (committed ~{committed}, "
                    f"reserved ~{held}, this call up to ~{estimate}); no call made"
                )
            rid = uuid.uuid4().hex
            data["reserved"][rid] = str(estimate)
            self._save(data)
        return Reservation(rid, estimate)

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
        with self._locked():
            data = self._load()
            data["reserved"].pop(reservation.reservation_id, None)
            total = Decimal(data["total_usd"]) + Decimal(cost)
            data["total_usd"] = str(total)
            data["calls"].append(
                {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "model": model,
                    "prompt_version": prompt_version,
                    "input_tokens": tokens[0],
                    "output_tokens": tokens[1],
                    "estimated_cost_usd": str(cost),
                    "reserved_usd": str(reservation.amount),
                    **({"note": note} if note else {}),
                }
            )
            self._save(data)
            return total

    def release(self, reservation: Reservation) -> None:
        with self._locked():
            data = self._load()
            data["reserved"].pop(reservation.reservation_id, None)
            self._save(data)


def try_reserve(ledger: str, cap: str, amount: str) -> bool:
    """Process-safe helper (used by the multi-process test): True if reserved."""
    try:
        SpendGuard(Decimal(cap), Path(ledger), lock_timeout=30).reserve(Decimal(amount))
        return True
    except SpendCapExceededError:
        return False
