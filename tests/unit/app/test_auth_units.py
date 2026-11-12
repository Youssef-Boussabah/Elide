"""P4 identity units (no database): roles, password hashing, CSRF derivation, audit guard,
and settlement rules of the file spend guard."""

from decimal import Decimal

import pytest

from api.ai.spend import (
    BudgetExceededError,
    ReservationConflictError,
    SpendGuard,
    UnknownReservationError,
)
from api.app.auth import passwords
from api.app.auth.permissions import ROLE_PERMISSIONS, Permission
from api.app.auth.service import csrf_for, sha256
from api.workflow.audit import _check_detail

P = Permission


def test_role_matrix_matches_adr_0011():
    assert ROLE_PERMISSIONS["owner"] == frozenset(P)
    assert P.PAYMENT_AUTHORIZE not in ROLE_PERMISSIONS["policy_editor"]
    assert P.OFFER_DECIDE not in ROLE_PERMISSIONS["policy_editor"]
    assert ROLE_PERMISSIONS["viewer"] == {P.WORKSPACE_READ}
    assert ROLE_PERMISSIONS["auditor"] == {P.WORKSPACE_READ, P.AUDIT_READ}
    assert all(P.MEMBERS_MANAGE not in v for k, v in ROLE_PERMISSIONS.items() if k != "owner")


def test_password_hashing_is_argon2id_and_verifies():
    h = passwords.hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert passwords.verify(h, "correct horse battery") == (True, None)
    assert passwords.verify(h, "wrong horse battery")[0] is False
    assert passwords.verify(None, "anything at all")[0] is False  # unknown user, same work
    with pytest.raises(passwords.WeakPasswordError):
        passwords.hash_password("short")


def test_csrf_token_is_derived_not_stored():
    a, b = csrf_for("token-a"), csrf_for("token-b")
    assert a != b and a == csrf_for("token-a") and len(a) == 64
    assert sha256(a) != a


@pytest.mark.parametrize("key", ["password", "session_token", "csrf", "Authorization", "api_key"])
def test_audit_detail_refuses_secret_fields(key):
    with pytest.raises(ValueError):
        _check_detail({"nested": [{key: "x"}]})


def test_audit_detail_allows_ids():
    _check_detail({"authorization_id": "pau_1", "terms_digest": "0" * 64})


def test_file_guard_settlement_is_idempotent_and_overage_blocks(tmp_path):
    g = SpendGuard(Decimal("0.010"), tmp_path / "ledger.json")
    r = g.reserve(Decimal("0.004"))
    g.commit(r, cost=Decimal("0.003"), model="m", prompt_version="v", tokens=(1, 1))
    g.commit(r, cost=Decimal("0.003"), model="m", prompt_version="v", tokens=(1, 1))  # no-op
    assert g.total() == Decimal("0.003")
    with pytest.raises(ReservationConflictError):
        g.commit(r, cost=Decimal("0.002"), model="m", prompt_version="v", tokens=(1, 1))
    with pytest.raises(ReservationConflictError):
        g.release(r)
    from api.ai.spend import Reservation

    with pytest.raises(UnknownReservationError):
        g.commit(
            Reservation("nope", Decimal(1)),
            cost=Decimal(0),
            model="m",
            prompt_version="v",
            tokens=(0, 0),
        )
    r2 = g.reserve(Decimal("0.006"))
    g.commit(r2, cost=Decimal("0.009"), model="m", prompt_version="v", tokens=(9, 9))
    assert g.total() == Decimal("0.012")  # recorded, never clipped
    with pytest.raises(BudgetExceededError):
        g.reserve(Decimal("0.000001"))
