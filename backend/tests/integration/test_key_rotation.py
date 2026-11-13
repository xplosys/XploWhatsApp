"""Phase 8: rotating the token-signing key and the secret encryption keys."""

from __future__ import annotations

import base64
import os
import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from typer.testing import CliRunner

from app.core.config import get_settings
from app.core.errors import Unauthorized
from app.core.security import crypto, jwt
from app.core.security.rotation import add_encryption_key, rewrap_secrets, rotate_jwt_key
from app.db.session import admin_transaction
from tests.conftest import Actor, RegisterFn
from tests.integration.test_ai import meta  # noqa: F401
from tests.integration.test_security import enable_mfa
from tests.integration.test_whatsapp import connect
from tests.support.meta import FakeMeta

API = "/api/v1"


def kek() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Real, versioned encryption keys for one test (the suite otherwise uses an ephemeral key)."""
    settings = get_settings()

    def use(value: str, active: str) -> None:
        monkeypatch.setattr(settings, "encryption_keys", SecretStr(value))
        monkeypatch.setattr(settings, "encryption_active_key_version", active)
        crypto._keks.cache_clear()

    yield use
    crypto._keks.cache_clear()


async def test_jwt_rotation_keeps_issued_tokens_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    old_pem = jwt.generate_private_key_pem()
    monkeypatch.setattr(settings, "jwt_private_key_pem", SecretStr(old_pem))
    monkeypatch.setattr(settings, "jwt_key_id", "k1")
    monkeypatch.setattr(settings, "jwt_previous_public_keys", None)
    jwt.get_keyset.cache_clear()
    old_token = jwt.encode_token({"sub": "u1"}, ttl_seconds=60)

    values = rotate_jwt_key("k1", old_pem)
    assert values["JWT_KEY_ID"].startswith("k2") and "\n" not in values["JWT_PRIVATE_KEY_PEM"]
    monkeypatch.setattr(settings, "jwt_private_key_pem", SecretStr(values["JWT_PRIVATE_KEY_PEM"]))
    monkeypatch.setattr(settings, "jwt_key_id", values["JWT_KEY_ID"])
    monkeypatch.setattr(settings, "jwt_previous_public_keys", SecretStr(values["JWT_PREVIOUS_PUBLIC_KEYS"]))
    jwt.get_keyset.cache_clear()
    try:
        # Tokens signed before the rotation still verify; new ones carry the new key ID.
        assert jwt.decode_token(old_token)["sub"] == "u1"
        new_token = jwt.encode_token({"sub": "u2"}, ttl_seconds=60)
        assert jwt.decode_token(new_token)["sub"] == "u2"
        import jwt as pyjwt

        assert pyjwt.get_unverified_header(new_token)["kid"] == values["JWT_KEY_ID"]
        # Once the old key is dropped from the previous keys, its tokens stop working.
        monkeypatch.setattr(settings, "jwt_previous_public_keys", None)
        jwt.get_keyset.cache_clear()
        with pytest.raises(Unauthorized):
            jwt.decode_token(old_token)
    finally:
        jwt.get_keyset.cache_clear()

    with pytest.raises(ValueError):
        rotate_jwt_key("k1", None)


def test_new_encryption_key_versions() -> None:
    values = add_encryption_key(f"v1:{kek()},v2:{kek()}")
    assert values["ENCRYPTION_ACTIVE_KEY_VERSION"] == "v3"
    assert [v.split(":")[0] for v in values["ENCRYPTION_KEYS"].split(",")] == ["v1", "v2", "v3"]
    with pytest.raises(ValueError):
        add_encryption_key("main:abc")
    with pytest.raises(ValueError):
        add_encryption_key(None)


async def test_rewrap_moves_every_secret_to_the_new_key(
    register: RegisterFn, meta: FakeMeta, keys: Any
) -> None:
    v1 = f"v1:{kek()}"
    keys(v1, "v1")
    owner: Actor = await register("o@acme.in", "Acme")
    await connect(owner)
    r = await owner.put(
        f"{API}/ai/providers/openai", json={"enabled": True, "api_key": "sk-test-0000000000000000"}
    )
    assert r.status_code == 200, r.text
    await enable_mfa(owner)

    async def versions() -> set[str]:
        async with admin_transaction() as s:
            rows = await s.execute(
                text(
                    "SELECT split_part(api_key_ciphertext, '.', 2) FROM ai_provider_configs "
                    "WHERE api_key_ciphertext IS NOT NULL UNION ALL "
                    "SELECT split_part(ciphertext, '.', 2) FROM whatsapp_credentials "
                    "WHERE ciphertext IS NOT NULL UNION ALL "
                    "SELECT split_part(mfa_secret_enc, '.', 2) FROM users WHERE mfa_secret_enc IS NOT NULL"
                )
            )
            return {v for (v,) in rows}

    assert await versions() == {"v1"}

    # Add v2 and make it active; v1 stays so old secrets can be read.
    added = add_encryption_key(v1)
    keys(added["ENCRYPTION_KEYS"], "v2")
    dry = await rewrap_secrets(dry_run=True)
    assert dry.ok and dry.rewrapped["whatsapp_credentials"] >= 1 and dry.rewrapped["users.mfa_secret"] == 1
    assert await versions() == {"v1"}

    report = await rewrap_secrets()
    assert report.ok and report.rewrapped["ai_provider_configs"] == 1
    assert await versions() == {"v2"}
    async with admin_transaction() as s:
        assert {v for (v,) in await s.execute(text("SELECT key_version FROM ai_provider_configs"))} == {"v2"}
    again = await rewrap_secrets()
    assert sum(again.rewrapped.values()) == 0 and again.already_current["users.mfa_secret"] == 1

    # With v1 removed, everything is still readable: the app keeps working.
    v2_only = ",".join(p for p in added["ENCRYPTION_KEYS"].split(",") if p.startswith("v2:"))
    keys(v2_only, "v2")
    providers = (await owner.get(f"{API}/ai/providers")).json()
    assert next(p for p in providers if p["provider"] == "openai")["ready"] is True


async def test_rewrap_refuses_when_a_secret_cannot_be_read(register: RegisterFn, keys: Any) -> None:
    keys(f"v1:{kek()}", "v1")
    owner = await register("o@acme.in", "Acme")
    await enable_mfa(owner)
    # The key that wrote the secret is missing (for example dropped too early).
    keys(f"v1:{kek()},v2:{kek()}", "v2")
    report = await rewrap_secrets()
    assert not report.ok and report.failed["users.mfa_secret"] == [owner.user_id]
    async with admin_transaction() as s:
        version = await s.scalar(
            text("SELECT split_part(mfa_secret_enc, '.', 2) FROM users WHERE id = :u"),
            {"u": uuid.UUID(owner.user_id)},
        )
    assert version == "v1"


def test_cli_prints_new_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli import cli

    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_private_key_pem", SecretStr(jwt.generate_private_key_pem()))
    monkeypatch.setattr(settings, "encryption_keys", SecretStr(f"v1:{kek()}"))
    runner = CliRunner()
    out = runner.invoke(cli, ["rotate-jwt-key"]).output
    assert "JWT_PRIVATE_KEY_PEM=" in out and "JWT_PREVIOUS_PUBLIC_KEYS=" in out
    out = runner.invoke(cli, ["new-encryption-key"]).output
    assert "ENCRYPTION_ACTIVE_KEY_VERSION=v2" in out
