"""Opaque random tokens. Only SHA-256 hashes are stored in the database."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
