from __future__ import annotations

import time

import pytest

from app.core.errors import Unauthorized
from app.core.ids import uuid7
from app.core.security import crypto, totp
from app.core.security.jwt import decode_token, encode_token
from app.core.security.passwords import hash_password, validate_password_strength, verify_password
from app.core.security.tokens import generate_token, hash_token


def test_password_hash_roundtrip() -> None:
    h = hash_password("Correct-Horse-42!")
    assert h.startswith("$argon2id$")
    assert verify_password("Correct-Horse-42!", h)
    assert not verify_password("wrong-password", h)
    assert not verify_password("anything", None)


def test_password_strength_rules() -> None:
    assert validate_password_strength("short")
    assert validate_password_strength("aaaaaaaaaaaa")
    assert validate_password_strength("me@example.com", "me@example.com")
    assert validate_password_strength("Correct-Horse-42!", "me@example.com") == []


def test_uuid7_is_version_7_and_time_ordered() -> None:
    ids = []
    for _ in range(5):
        ids.append(uuid7())
        time.sleep(0.002)
    assert all(i.version == 7 for i in ids)
    assert ids == sorted(ids)


def test_tokens_are_random_and_hashed() -> None:
    a, b = generate_token(), generate_token()
    assert a != b and len(a) >= 40
    assert hash_token(a) == hash_token(a) and len(hash_token(a)) == 64


def test_jwt_roundtrip_and_type_check() -> None:
    token = encode_token({"sub": "u1", "tid": None}, ttl_seconds=60)
    claims = decode_token(token)
    assert claims["sub"] == "u1" and claims["typ"] == "access"
    with pytest.raises(Unauthorized):
        decode_token(token, expected_type="mfa_challenge")


def test_jwt_expired_and_tampered() -> None:
    expired = encode_token({"sub": "u1"}, ttl_seconds=-10)
    with pytest.raises(Unauthorized) as exc:
        decode_token(expired)
    assert exc.value.code == "token_expired"
    good = encode_token({"sub": "u1"}, ttl_seconds=60)
    head, body, sig = good.split(".")
    with pytest.raises(Unauthorized):
        decode_token(f"{head}.{body}.{sig[:-4]}AAAA")


def test_envelope_encryption_roundtrip_and_aad() -> None:
    token = crypto.encrypt("meta-access-token", "whatsapp.credential")
    assert "meta-access-token" not in token
    assert crypto.decrypt(token, "whatsapp.credential") == "meta-access-token"
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token, "different-context")
    # Two encryptions of the same value differ (fresh data key and nonce).
    assert crypto.encrypt("x") != crypto.encrypt("x")


def test_key_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    old = crypto.generate_kek()
    monkeypatch.setenv("ENCRYPTION_KEYS", f"v1:{old}")
    monkeypatch.setenv("ENCRYPTION_ACTIVE_KEY_VERSION", "v1")
    get_settings.cache_clear()
    crypto._keks.cache_clear()
    token_v1 = crypto.encrypt("secret")
    assert crypto.key_version_of(token_v1) == "v1"

    monkeypatch.setenv("ENCRYPTION_KEYS", f"v1:{old},v2:{crypto.generate_kek()}")
    monkeypatch.setenv("ENCRYPTION_ACTIVE_KEY_VERSION", "v2")
    get_settings.cache_clear()
    crypto._keks.cache_clear()
    rewrapped = crypto.rewrap(token_v1)
    assert crypto.key_version_of(rewrapped) == "v2"
    assert crypto.decrypt(rewrapped) == crypto.decrypt(token_v1) == "secret"
    get_settings.cache_clear()
    crypto._keks.cache_clear()


def test_mask_secret() -> None:
    assert crypto.mask_secret("sk-abcdef123456") == "********3456"


def test_totp_and_recovery_codes() -> None:
    import pyotp

    secret = totp.new_secret()
    assert totp.verify_code(secret, pyotp.TOTP(secret).now())
    assert not totp.verify_code(secret, "000000x")
    codes, hashes = totp.generate_recovery_codes()
    assert len(codes) == 10 and hashes[0] == hash_token(codes[0])
