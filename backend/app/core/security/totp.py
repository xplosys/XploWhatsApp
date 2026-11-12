"""TOTP (RFC 6238) multi-factor authentication helpers."""

from __future__ import annotations

import secrets

import pyotp

from app.core.security.tokens import hash_token

ISSUER = "XploWhatsApp"


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=ISSUER)


def verify_code(secret: str, code: str) -> bool:
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return bool(pyotp.TOTP(secret).verify(code, valid_window=1))


def generate_recovery_codes(count: int = 10) -> tuple[list[str], list[str]]:
    """Returns (plain codes to show once, hashes to store)."""
    codes = [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(count)]
    return codes, [hash_token(c) for c in codes]
