"""Argon2id password hashing (argon2-cffi, RFC 9106 parameters by default).

`verify` always performs one Argon2 verification, also for unknown users (against a
dummy hash), so response time does not reveal whether a login exists.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 1024  # bound hashing work per request

_HASHER = PasswordHasher()
_DUMMY_HASH = _HASHER.hash("elide-dummy-password-that-is-never-valid")


class WeakPasswordError(ValueError):
    pass


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise WeakPasswordError(f"password must be {MIN_LENGTH}-{MAX_LENGTH} characters")
    return _HASHER.hash(password)


def verify(stored_hash: str | None, password: str) -> tuple[bool, str | None]:
    """(valid, replacement hash if the parameters changed, else None)."""
    if not isinstance(password, str) or len(password) > MAX_LENGTH:
        password = ""
    target = stored_hash or _DUMMY_HASH
    try:
        _HASHER.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    if stored_hash is None:
        return False, None
    if _HASHER.check_needs_rehash(stored_hash):
        return True, _HASHER.hash(password)
    return True, None
