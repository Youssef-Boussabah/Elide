"""The lexicographic objective, defined once over *verified* metrics.

Hard constraints (consent, currency, reserves, deadlines, availability, revisions and each
merchant's own acceptance rules) are never in the objective; a plan that breaks one is
infeasible, not penalised. Among feasible plans, larger is better, tier by tier:

    (lead_0, lead_1, on_time_face, surplus, -early_cash, -cent_seconds, -fees)

- NETWORK:       lead = (0, 0)
- FOCAL:         lead = (focal merchant's own net benefit, 0)
- MIN_LIQUIDITY: lead = (-hypothetical funding, -existing own-funds draw)
- on_time_face:  original face of invoices paid by their due slot. The baseline is fixed
                 for a given network, so maximising this maximises the increment over it.
- surplus:       sum of stated early-receipt values minus payers' time cost of paying
                 early. Concessions cancel out (a transfer between two merchants).
- early_cash:    cash debited before the due slot; cent_seconds: cash x time advanced
                 (liquidity-dollar-days x 8,640,000); fees: all leg fees.
Remaining ties: the lexicographically smallest schedule ((slot, leg_id), ...).

The solver computes this vector itself; `objective_value` recomputes it from the
independent verifier's replay so the two can be compared.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.planning.verifier import Metrics


class Objective(StrEnum):
    NETWORK = "network"
    FOCAL = "focal_merchant"
    MIN_LIQUIDITY = "min_liquidity"


TIERS = (
    "lead_0",
    "lead_1",
    "on_time_face_cents",
    "surplus_cents",
    "-early_cash_cents",
    "-cent_seconds",
    "-fees_cents",
)


def objective_value(metrics: Metrics, objective: Objective) -> tuple[int, ...]:
    if objective is Objective.FOCAL:
        lead = (metrics.focal_net, 0)
    elif objective is Objective.MIN_LIQUIDITY:
        lead = (
            -sum(m.hypothetical_need_cents for m in metrics.merchants),
            -sum(m.existing_draw_cents for m in metrics.merchants),
        )
    else:
        lead = (0, 0)
    return (
        *lead,
        metrics.on_time_face,
        metrics.surplus,
        -metrics.early_cash,
        -metrics.cent_seconds,
        -metrics.fees,
    )
