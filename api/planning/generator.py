"""Seeded SYNTHETIC network generator for property tests and benchmarks (doc 12).

`generate(family, seed)` is a pure function of its arguments (string-seeded
`random.Random`, independent of PYTHONHASHSEED). Families cover chains, competing
candidates, cycles, disconnected parts, parallel invoices, scarce cash, fees and
availability lags. Some networks include disputes, refusals, non-participants, payee
concession caps, conditional fees and negotiated proposals, so baselines and the
planner see zero-lift and negative cases as well as wins. Every generated object is
labelled SYNTHETIC with `GENERATOR_VERSION`.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from api.domain.invoice import DisputeStatus, Provenance, SourceKind
from api.domain.money import USD, Money
from api.planning.builder import synthetic_position
from api.planning.network import (
    Authorization,
    AvailabilityModel,
    ConsentBasis,
    FeeStatus,
    FeeTerms,
    MerchantPolicy,
    Network,
    ObligationEdge,
    PaymentOption,
    SlotClock,
    TermsKind,
)

GENERATOR_VERSION = "p2-gen-1"
FAMILIES = (
    "chain",
    "two_candidates",
    "cycle",
    "disconnected",
    "parallel",
    "scarce",
    "fees",
    "lagged",
)
START = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
SYN = Provenance(SourceKind.SYNTHETIC_FIXTURE, GENERATOR_VERSION, START)


def _usd(c: int) -> Money:
    return Money(c, USD)


def _edges(family: str, rng: random.Random, n_obl: int) -> list[tuple[int, int]]:
    if family == "cycle":
        k = max(2, min(n_obl, 4))
        base = [(i, (i + 1) % k) for i in range(k)]
    elif family == "two_candidates":
        base = [(0, 1), (0, 2), (1, 3), (3, 4)]
    elif family == "disconnected":
        base = [(0, 1), (2, 3), (1, 4), (3, 5)]
    elif family == "parallel":
        base = [(0, 1), (0, 1), (1, 2), (2, 3)]
    else:
        base = [(i, i + 1) for i in range(5)]
    edges = base[:n_obl]
    while len(edges) < n_obl:
        a, b = rng.sample(range(5), 2)
        edges.append((a, b))
    return edges


def generate(family: str, seed: int, *, max_obligations: int = 4, slots: int = 3) -> Network:
    if family not in FAMILIES:
        raise ValueError(f"unknown family {family!r}")
    # synthetic test data, not security-sensitive
    rng = random.Random(f"{GENERATOR_VERSION}:{family}:{seed}:{max_obligations}:{slots}")  # noqa: S311
    clock = SlotClock.uniform(START, timedelta(days=1), slots)
    n_obl = rng.randint(2, max(2, max_obligations))
    edges = _edges(family, rng, n_obl)
    merchants = sorted({f"m{i}" for e in edges for i in e} | {"m0"})
    scarce = family == "scarce"
    positions = []
    for m in merchants:
        if m == "m0":
            cash = rng.choice([40000, 80000, 120000, 200000]) // (3 if scarce else 1)
            reserve = rng.choice([0, 5000, 10000, 20000])
        else:
            cash = rng.choice([0, 0, 0, 10000, 30000]) // (3 if scarce else 1)
            reserve = rng.choice([0, 0, 0, 5000, 15000])
        positions.append(synthetic_position(m, cash, reserve))
    policies = []
    for m in merchants:
        policies.append(
            MerchantPolicy(
                m,
                participating=rng.random() > 0.05,
                hurdle_bps=rng.choice([0, 0, 500, 1500]) if m == "m0" else rng.choice([0, 0, 300]),
                min_early_discount=_usd(rng.choice([0, 0, 500, 1000]) if m == "m0" else 0),
                early_receipt_value_bps=rng.choice([0, 0, 2000, 5000]),
                max_concession_given=rng.choice([None, None, _usd(1500)]),
                max_fee_per_leg=rng.choice([None, None, _usd(1500)]),
                source=f"synthetic:{GENERATOR_VERSION}",
            )
        )
    lag = AvailabilityModel(
        "synthetic_delay" if family == "lagged" else "synthetic_zero_lag",
        default_lag=rng.choice([1, 2]) if family == "lagged" else 1,
    )
    obligations, options = [], []
    for k, (a, b) in enumerate(edges):
        oid = f"o{k}"
        debtor, creditor = f"m{a}", f"m{b}"
        face = rng.choice([10000, 20000, 30000, 50000, 70000, 90000])
        due = START + timedelta(days=rng.randint(1, slots), hours=rng.choice([0, 6]))
        dispute = DisputeStatus.OPEN if rng.random() < 0.05 else DisputeStatus.NONE
        obligations.append(
            ObligationEdge(
                oid, debtor, creditor, _usd(face), _usd(0), _usd(0), 1, due, SYN, dispute
            )
        )
        if family == "fees" or rng.random() < 0.2:
            payee_fee = rng.choice([0, 300, 900])
            payer_fee = rng.choice([0, 0, 200])
            status = FeeStatus.CONDITIONAL if rng.random() < 0.3 else FeeStatus.KNOWN
            fee = FeeTerms(_usd(payer_fee), _usd(payee_fee), status, "synthetic")
        else:
            fee = FeeTerms()
        basis = ConsentBasis.REFUSED if rng.random() < 0.05 else ConsentBasis.SYNTHETIC_ASSUMPTION
        pay = Authorization(debtor, "authorize_payment", basis)
        options.append(
            PaymentOption(
                f"{oid}:r1",
                oid,
                1,
                TermsKind.IN_FORCE,
                debtor,
                creditor,
                _usd(face),
                _usd(0),
                fee,
                (pay,),
                SYN,
            )
        )
        if rng.random() < 0.5:
            disc = rng.choice([500, 1000, 2000])
            deadline = START + timedelta(days=rng.randint(0, max(0, slots - 2)), hours=12)
            options.append(
                PaymentOption(
                    f"{oid}:r2:offer",
                    oid,
                    2,
                    TermsKind.OPEN_OFFER,
                    debtor,
                    creditor,
                    _usd(face - disc),
                    _usd(disc),
                    fee,
                    (pay, Authorization(debtor, "accept_offer", basis)),
                    SYN,
                    deadline,
                )
            )
        if rng.random() < 0.3:
            disc = rng.choice([1000, 3000])
            options.append(
                PaymentOption(
                    f"{oid}:r3:proposal",
                    oid,
                    3,
                    TermsKind.NEGOTIATED_PROPOSAL,
                    debtor,
                    creditor,
                    _usd(face - disc),
                    _usd(disc),
                    fee,
                    (
                        pay,
                        Authorization(debtor, "sign_terms", ConsentBasis.SYNTHETIC_ASSUMPTION),
                        Authorization(creditor, "sign_terms", ConsentBasis.SYNTHETIC_ASSUMPTION),
                    ),
                    SYN,
                    START + timedelta(days=slots),
                )
            )
    return Network(
        network_id=f"gen_{family}_{seed}",
        as_of=START,
        clock=clock,
        availability=lag,
        focal_merchant="m0",
        positions=tuple(positions),
        policies=tuple(policies),
        obligations=tuple(obligations),
        options=tuple(options),
        labels=("SYNTHETIC_NETWORK", f"GENERATOR:{GENERATOR_VERSION}:{family}:{seed}"),
    )
