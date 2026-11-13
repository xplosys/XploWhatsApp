"""Key rotation helpers behind the `rotate-jwt-key`, `new-encryption-key` and
`rewrap-secrets` commands. See docs/SECURITY.md for the runbook."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from app.core.security import crypto
from app.core.security.jwt import generate_private_key_pem


def _escape(pem: str) -> str:
    """One-line form for .env files (the loaders turn \\n back into newlines)."""
    return pem.strip().replace("\n", "\\n")


def _public_pem(private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.replace("\\n", "\n").encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("JWT_PRIVATE_KEY_PEM must be an EC P-256 private key")
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def rotate_jwt_key(current_kid: str, current_pem: str | None, now: datetime | None = None) -> dict[str, str]:
    """A new signing key. The current public key moves to JWT_PREVIOUS_PUBLIC_KEYS so
    tokens already issued keep working until they expire (15 minutes by default). Only
    the one previous key is kept: older ones are long past every token's lifetime."""
    if not current_pem:
        raise ValueError("Set JWT_PRIVATE_KEY_PEM before rotating: there is no current key to retire.")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M")
    new_kid = f"k{stamp}"
    if new_kid == current_kid:
        raise ValueError("Wait a minute between rotations.")
    return {
        "JWT_PRIVATE_KEY_PEM": _escape(generate_private_key_pem()),
        "JWT_KEY_ID": new_kid,
        "JWT_PREVIOUS_PUBLIC_KEYS": json.dumps({current_kid: _escape(_public_pem(current_pem))}),
    }


_VERSION = re.compile(r"^v(\d+)$")


def add_encryption_key(existing: str | None) -> dict[str, str]:
    """Append a new key-encryption key and make it active. Old versions stay listed
    so existing secrets can still be read until `rewrap-secrets` has moved them."""
    if not existing:
        raise ValueError("Set ENCRYPTION_KEYS before rotating: there is no current key.")
    versions = []
    for item in existing.split(","):
        version = item.strip().partition(":")[0]
        match = _VERSION.match(version)
        if not match:
            raise ValueError(f"Unexpected key version {version!r}; versions look like v1, v2.")
        versions.append(int(match.group(1)))
    new_version = f"v{max(versions) + 1}"
    return {
        "ENCRYPTION_KEYS": f"{existing.strip()},{new_version}:{crypto.generate_kek()}",
        "ENCRYPTION_ACTIVE_KEY_VERSION": new_version,
    }


@dataclass
class RewrapReport:
    active_version: str
    rewrapped: dict[str, int] = field(default_factory=dict)
    already_current: dict[str, int] = field(default_factory=dict)
    failed: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(self.failed.values())


async def rewrap_secrets(dry_run: bool = False) -> RewrapReport:
    """Re-encrypt every stored secret under the active key-encryption key: business AI
    keys, WhatsApp access tokens and two-factor secrets. Each secret is decrypted with its
    own (older) key and encrypted again; its associated data is unchanged. Runs on the
    admin role in one transaction, so it either completes or changes nothing."""
    from app.db.session import admin_transaction

    active, _ = crypto._keks()
    report = RewrapReport(active_version=active)

    def tally(name: str, token: str, aad: str, row_id: object) -> str | None:
        report.rewrapped.setdefault(name, 0)
        report.already_current.setdefault(name, 0)
        report.failed.setdefault(name, [])
        if crypto.key_version_of(token) == active:
            report.already_current[name] += 1
            return None
        try:
            new = crypto.rewrap(token, aad)
        except crypto.DecryptionError:
            report.failed[name].append(str(row_id))
            return None
        report.rewrapped[name] += 1
        return new

    try:
        async with admin_transaction() as session:
            await _rewrap_all(session, tally, active)
            if dry_run or not report.ok:
                raise _Abort()
    except _Abort:
        pass
    return report


class _Abort(Exception):
    """Rolls the rewrap transaction back (dry run, or a secret that couldn't be read)."""


async def _rewrap_all(
    session: Any, tally: Callable[[str, str, str, object], str | None], active: str
) -> None:
    from app.modules.ai.models import AIProviderConfig
    from app.modules.ai.registry import _aad as ai_aad
    from app.modules.auth.models import User
    from app.modules.auth.service import MFA_SECRET_AAD
    from app.modules.whatsapp.models import WhatsAppCredential
    from app.modules.whatsapp.service import _aad as whatsapp_aad

    configs = select(AIProviderConfig).where(AIProviderConfig.api_key_ciphertext.is_not(None))
    for cfg in (await session.execute(configs.with_for_update())).scalars():
        aad = ai_aad(cfg.tenant_id, cfg.provider)
        new = tally("ai_provider_configs", cfg.api_key_ciphertext, aad, cfg.id)
        if new:
            cfg.api_key_ciphertext = new
            cfg.key_version = active
    creds = select(WhatsAppCredential).where(WhatsAppCredential.ciphertext.is_not(None))
    for cred in (await session.execute(creds.with_for_update())).scalars():
        aad = whatsapp_aad(cred.tenant_id, cred.account_id, cred.kind)
        new = tally("whatsapp_credentials", cred.ciphertext, aad, cred.id)
        if new:
            cred.ciphertext = new
            cred.key_version = active
    users = select(User).where(User.mfa_secret_enc.is_not(None))
    for user in (await session.execute(users.with_for_update())).scalars():
        new = tally("users.mfa_secret", user.mfa_secret_enc, MFA_SECRET_AAD, user.id)
        if new:
            user.mfa_secret_enc = new
