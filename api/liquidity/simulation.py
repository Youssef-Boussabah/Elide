"""Checked sequential cash transfers for SYNTHETIC scenarios (not a planner).

Used to demonstrate and test cash conservation and reserve enforcement on fixtures such
as the canonical buyer -> food supplier -> carrier -> mechanic chain. Assumptions are
explicit: `availability_model="synthetic_zero_lag"` means a received amount is usable by
the next transfer in the list (never within the same transfer). Nothing here is provider
evidence; results carry the SYNTHETIC label.

Invariants enforced on every transfer:
- payer cash after the leg and its fee must stay >= payer's protected reserve;
- amounts are USD Money; fees leave the network to an explicit `provider_fees` total;
- sum(closing cash) + fees == sum(opening cash) (conservation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from api.domain.invoice import check_id
from api.domain.money import USD, Money, require_spendable


class SimulationError(ValueError):
    pass


class ReserveViolationError(SimulationError):
    pass


@dataclass(frozen=True, slots=True)
class Transfer:
    leg_id: str
    payer: str
    payee: str
    amount: Money
    fee: Money | None = None  # charged to the payer

    def __post_init__(self) -> None:
        check_id(self.leg_id, "leg_id")
        if self.payer == self.payee:
            raise SimulationError("payer and payee must differ")
        require_spendable(self.amount)
        if self.amount.cents == 0:
            raise SimulationError("transfer amount must be positive")
        if self.fee is not None:
            require_spendable(self.fee)


@dataclass(frozen=True, slots=True)
class SimulationResult:
    closing: Mapping[str, Money]
    gross_flow: Money
    provider_fees: Money
    legs: tuple[str, ...]
    label: str = "SYNTHETIC"
    availability_model: str = "synthetic_zero_lag"


def simulate(
    opening: Mapping[str, Money],
    reserves: Mapping[str, Money],
    transfers: Sequence[Transfer],
) -> SimulationResult:
    cash = {m: require_spendable(v) for m, v in opening.items()}
    fees = Money.zero(USD)
    gross = Money.zero(USD)
    seen: set[str] = set()
    for t in transfers:
        if t.leg_id in seen:
            raise SimulationError(f"leg {t.leg_id} appears twice")
        seen.add(t.leg_id)
        if t.payer not in cash or t.payee not in cash:
            raise SimulationError(f"unknown merchant in leg {t.leg_id}")
        fee = t.fee or Money.zero(USD)
        needed = t.amount + fee + reserves.get(t.payer, Money.zero(USD))
        if cash[t.payer] < needed:
            short = needed - cash[t.payer]
            raise ReserveViolationError(
                f"{t.leg_id}: {t.payer} has {cash[t.payer]}, needs {t.amount} + fee {fee} "
                f"+ reserve {reserves.get(t.payer, Money.zero(USD))}; short by {short}"
            )
        cash[t.payer] = cash[t.payer] - t.amount - fee
        cash[t.payee] = cash[t.payee] + t.amount
        fees = fees + fee
        gross = gross + t.amount

    opening_total = Money.total(opening.values(), USD)
    closing_total = Money.total(cash.values(), USD)
    if closing_total + fees != opening_total:
        raise AssertionError("cash conservation broken")
    return SimulationResult(dict(cash), gross, fees, tuple(t.leg_id for t in transfers))
