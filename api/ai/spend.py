"""Hard spend cap for real model calls (owner-authorized budget: USD 10.00 for P3).

Before each call the guard reserves an upper-bound estimate (generous input-token
estimate + the full `max_tokens`, priced at the configured rates x a safety factor) and
refuses if the running total would pass the cap. After the call it records the estimate
recomputed from the provider's reported usage. The ledger lives in `.elide/` (untracked)
so the cap holds across runs. These are ESTIMATES; the provider's console is the bill.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path

from api.ai.adapter import LLMUnavailableError

DEFAULT_CAP_USD = Decimal("10.00")
SAFETY_FACTOR = Decimal("1.5")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / ".elide" / "llm_spend.json"


class SpendCapExceededError(LLMUnavailableError):
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


class SpendGuard:
    def __init__(self, cap_usd: Decimal = DEFAULT_CAP_USD, ledger: Path = DEFAULT_LEDGER) -> None:
        self.cap = cap_usd
        self.ledger = ledger
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.ledger.exists():
            return {"total_usd": "0", "calls": []}
        return json.loads(self.ledger.read_text("utf-8"))

    def total(self) -> Decimal:
        return Decimal(self._load()["total_usd"])

    def check(self, estimate: Decimal) -> None:
        with self._lock:
            total = self.total()
            if total + estimate > self.cap:
                raise SpendCapExceededError(
                    f"spend cap USD {self.cap} would be exceeded (spent ~{total}, this call "
                    f"up to ~{estimate}); no call made"
                )

    def record(self, *, model: str, prompt_version: str, cost: Decimal, tokens: tuple[int, int]):
        with self._lock:
            data = self._load()
            total = Decimal(data["total_usd"]) + cost
            data["total_usd"] = str(total)
            data["calls"].append(
                {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "model": model,
                    "prompt_version": prompt_version,
                    "input_tokens": tokens[0],
                    "output_tokens": tokens[1],
                    "estimated_cost_usd": str(cost),
                }
            )
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(json.dumps(data, indent=1), "utf-8")
            return total
