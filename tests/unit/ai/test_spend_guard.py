"""Atomic spend reservation: threads and processes cannot exceed the software cap.

These tests exercise the LOCAL estimate-based cap only; the provider's billing is the
authoritative record and is not tested here.
"""

from __future__ import annotations

import threading
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from api.ai.adapter import LLMUnavailableError
from api.ai.openai_compat import NEBIUS_BASE_URL, OpenAICompatibleAdapter
from api.ai.spend import Prices, SpendCapExceededError, SpendGuard, SpendLockError, try_reserve


def test_overlapping_calls_cannot_both_take_the_last_budget(tmp_path):
    """Old check-then-record API: two overlapping calls both passed `check`. Now the
    second reservation sees the first one before any record exists."""
    g = SpendGuard(Decimal("0.006"), tmp_path / "l.json")
    g.reserve(Decimal("0.004"))
    with pytest.raises(SpendCapExceededError):
        g.reserve(Decimal("0.004"))
    assert g.outstanding() == Decimal("0.004") and g.total() == 0


def test_parallel_threads_never_reserve_more_than_the_cap(tmp_path):
    g = SpendGuard(Decimal("0.010"), tmp_path / "l.json")
    barrier = threading.Barrier(16)
    won, lost = [], []

    def worker():
        barrier.wait()
        try:
            won.append(g.reserve(Decimal("0.004")))
        except SpendCapExceededError:
            lost.append(1)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(won) == 2 and len(lost) == 14
    assert g.outstanding() == Decimal("0.008") <= g.cap


def test_separate_processes_share_the_cap(tmp_path):
    ledger = str(tmp_path / "shared.json")
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(try_reserve, [ledger] * 8, ["0.010"] * 8, ["0.004"] * 8))
    assert sum(results) == 2
    assert SpendGuard(Decimal("0.010"), tmp_path / "shared.json").outstanding() == Decimal("0.008")


def test_commit_replaces_and_release_frees_the_reservation(tmp_path):
    g = SpendGuard(Decimal("0.010"), tmp_path / "l.json")
    r1 = g.reserve(Decimal("0.004"))
    r2 = g.reserve(Decimal("0.004"))
    g.commit(r1, cost=Decimal("0.001"), model="m", prompt_version="p", tokens=(10, 5))
    g.release(r2)
    assert (g.total(), g.outstanding()) == (Decimal("0.001"), Decimal(0))
    g.reserve(Decimal("0.009"))  # freed budget is usable again


def test_a_held_lock_fails_closed(tmp_path):
    g = SpendGuard(Decimal("1"), tmp_path / "l.json", lock_timeout=0.05)
    g.lock_path.write_text("other-process")
    with pytest.raises(SpendLockError):
        g.reserve(Decimal("0.001"))
    assert not g.ledger.exists()  # nothing reserved, nothing sent


class Out(BaseModel):
    x: int


def _adapter(tmp_path, handler):
    return OpenAICompatibleAdapter(
        provider="nebius",
        base_url=NEBIUS_BASE_URL,
        api_key=SecretStr("nb-test-key-not-real"),
        model="m",
        prices=Prices(Decimal("0.20"), Decimal("0.60"), "test"),
        spend=SpendGuard(Decimal("1.00"), tmp_path / "l.json"),
        transport=httpx.MockTransport(handler),
    )


def test_http_error_releases_and_unknown_outcome_commits_the_estimate(tmp_path):
    a = _adapter(tmp_path, lambda req: httpx.Response(500, json={}))
    with pytest.raises(LLMUnavailableError):
        a.generate_structured(system="s", user="u", schema=Out)
    g = SpendGuard(Decimal("1.00"), tmp_path / "l.json")
    assert (g.total(), g.outstanding()) == (0, 0)

    def timeout(req):
        raise httpx.ReadTimeout("slow")

    b = _adapter(tmp_path, timeout)
    with pytest.raises(LLMUnavailableError):
        b.generate_structured(system="s", user="u", schema=Out, max_tokens=100)
    assert g.total() > 0 and g.outstanding() == 0  # kept at the full estimate


def test_a_lock_file_being_deleted_is_waited_for_not_an_error(tmp_path, monkeypatch):
    """Windows: creating the lock while another process deletes it raises PermissionError
    (delete pending). Seen intermittently in the multi-process test on 2026-11-12."""
    import api.ai.spend as spend

    real_open = spend.os.open
    calls = {"n": 0}

    def flaky_open(path, flags, *args):
        if str(path).endswith(".lock") and calls["n"] < 2:
            calls["n"] += 1
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(spend.os, "open", flaky_open)
    g = SpendGuard(Decimal("1"), tmp_path / "l.json", lock_timeout=2)
    g.reserve(Decimal("0.001"))
    assert calls["n"] == 2 and g.outstanding() == Decimal("0.001")


def test_a_persistent_permission_error_still_fails_closed(tmp_path, monkeypatch):
    import api.ai.spend as spend

    def denied(path, flags, *args):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(spend.os, "open", denied)
    g = SpendGuard(Decimal("1"), tmp_path / "l.json", lock_timeout=0.05)
    with pytest.raises(SpendLockError):
        g.reserve(Decimal("0.001"))
