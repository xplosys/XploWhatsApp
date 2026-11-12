"""Envelope encryption for secrets stored in the database.

Each secret is encrypted with a fresh random 256-bit data key (AES-256-GCM).
The data key is wrapped with a versioned key-encryption key (KEK) supplied via
ENCRYPTION_KEYS. The stored token records the KEK version so keys can be rotated:
add a new version, make it active, then run the re-wrap task.

Format: "xenc1.<kek_version>.<b64 wrap_nonce+wrapped_dk>.<b64 nonce+ciphertext>"
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_PREFIX = "xenc1"


class DecryptionError(Exception):
    pass


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def generate_kek() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@lru_cache
def _keks() -> tuple[str, dict[str, bytes]]:
    settings = get_settings()
    if settings.encryption_keys is None:
        structlog.get_logger().warning("encryption_key_generated_ephemeral", reason="ENCRYPTION_KEYS not set")
        return settings.encryption_active_key_version, {
            settings.encryption_active_key_version: os.urandom(32)
        }
    keys: dict[str, bytes] = {}
    for item in settings.encryption_keys.get_secret_value().split(","):
        version, _, b64 = item.strip().partition(":")
        key = base64.b64decode(b64)
        if len(key) != 32:
            raise ValueError(f"Encryption key {version} must be 32 bytes")
        keys[version] = key
    if settings.encryption_active_key_version not in keys:
        raise ValueError("ENCRYPTION_ACTIVE_KEY_VERSION is not present in ENCRYPTION_KEYS")
    return settings.encryption_active_key_version, keys


def encrypt(plaintext: str, associated_data: str = "") -> str:
    version, keys = _keks()
    data_key = AESGCM.generate_key(bit_length=256)
    wrap_nonce, nonce = os.urandom(12), os.urandom(12)
    wrapped = AESGCM(keys[version]).encrypt(wrap_nonce, data_key, version.encode())
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext.encode(), associated_data.encode())
    return ".".join([_PREFIX, version, _b64e(wrap_nonce + wrapped), _b64e(nonce + ciphertext)])


def decrypt(token: str, associated_data: str = "") -> str:
    try:
        prefix, version, wrapped_b64, body_b64 = token.split(".")
        if prefix != _PREFIX:
            raise DecryptionError("Unknown ciphertext format")
        _, keys = _keks()
        kek = keys[version]
        wrapped, body = _b64d(wrapped_b64), _b64d(body_b64)
        data_key = AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], version.encode())
        return AESGCM(data_key).decrypt(body[:12], body[12:], associated_data.encode()).decode()
    except DecryptionError:
        raise
    except Exception as exc:
        raise DecryptionError("Could not decrypt secret") from exc


def key_version_of(token: str) -> str:
    return token.split(".")[1]


def rewrap(token: str, associated_data: str = "") -> str:
    """Re-encrypt under the active KEK (used by key rotation)."""
    return encrypt(decrypt(token, associated_data), associated_data)


def mask_secret(value: str, visible: int = 4) -> str:
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * 8 + value[-visible:]
