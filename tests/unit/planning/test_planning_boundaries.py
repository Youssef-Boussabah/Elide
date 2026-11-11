"""The planner is pure: no PayPal client, no database, no network, no AI; and the P1
`open_offers` helper it relies on is read-only and honours the offer lifecycle."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from api.domain.invoice import RevisionStatus
from fixtures import canonical

PLANNING = Path(__file__).resolve().parents[3] / "api" / "planning"
FORBIDDEN = ("api.integrations", "httpx", "sqlalchemy", "api.ai", "api.accounting.store")


def test_planning_package_has_no_payment_db_or_network_access():
    for py in PLANNING.rglob("*.py"):
        source = py.read_text(encoding="utf-8")
        for name in FORBIDDEN:
            assert f"import {name}" not in source and f"from {name}" not in source, (py, name)


def test_open_offers_lists_only_unexpired_unclosed_drafts():
    rec = canonical.scenario_b_proposed_ten(canonical.scenario_b_supplier_offer())
    at = canonical.AS_OF + timedelta(minutes=30)
    assert [r.revision_no for r in rec.open_offers(at)] == [2, 3]
    # after the deadlines both are expired
    assert rec.open_offers(canonical.AS_OF + timedelta(minutes=90)) == ()
    # accepting rev 3 closes it; rev 2 stays open until it expires
    accepted = canonical.scenario_b_authorize(rec, minutes=20)
    assert [r.revision_no for r in accepted.open_offers(at)] == [2]
    assert accepted.revisions[-1].status is RevisionStatus.AUTHORIZED
    # an offer made after `at` is not open yet
    assert rec.open_offers(canonical.AS_OF + timedelta(minutes=7)) == rec.revisions[1:2]
