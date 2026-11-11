"""Import one provider settlement into the journal as a single accounting bundle.

The invoice-issued, discount and capture entries describe one economic event. They are
booked in one transaction (`post_entries`), so an interruption books nothing and a retry
books everything once. Whether a settlement is reconciled is **derived** from the journal
(`settlement_import_status`): COMPLETE only when every entry of the bundle is booked with
exactly the expected content. A partly booked bundle is PARTIAL, never "reconciled".
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from api.accounting.settlement_postings import settlement_entries
from api.accounting.store import BundleStatus, PostResult, bundle_status, post_entries
from api.domain.settlement import ProviderSettlement

__all__ = ["BundleStatus", "SettlementImport", "import_settlement", "settlement_import_status"]


@dataclass(frozen=True, slots=True)
class SettlementImport:
    capture_id: str
    status: BundleStatus
    results: tuple[PostResult, ...]


def import_settlement(
    engine: Engine, settlement: ProviderSettlement, entity_id: str
) -> SettlementImport:
    entries = settlement_entries(settlement, entity_id)
    results = post_entries(engine, entries)
    status = bundle_status(engine, entries)
    if status is not BundleStatus.COMPLETE:  # cannot happen after a committed bundle
        raise AssertionError(f"settlement {settlement.capture_id} imported but {status.value}")
    return SettlementImport(settlement.capture_id, status, results)


def settlement_import_status(
    engine: Engine, settlement: ProviderSettlement, entity_id: str
) -> BundleStatus:
    return bundle_status(engine, settlement_entries(settlement, entity_id))
