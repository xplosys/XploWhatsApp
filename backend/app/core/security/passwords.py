"""Argon2id password hashing."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP-recommended Argon2id baseline; tune time_cost on the production VPS.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32, salt_len=16)

# A real hash used to equalise timing when the user does not exist.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_strength(password: str, email: str | None = None) -> list[str]:
    problems: list[str] = []
    if len(password) < 10:
        problems.append("Password must be at least 10 characters long.")
    if len(password) > 128:
        problems.append("Password must be at most 128 characters long.")
    if len(set(password)) < 5:
        problems.append("Password is too repetitive.")
    if email and password.lower() == email.lower():
        problems.append("Password must not be your email address.")
    return problems
