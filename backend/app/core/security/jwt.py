"""ES256 JWT access tokens with key IDs for rotation."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Settings, get_settings
from app.core.errors import Unauthorized

ALGORITHM = "ES256"


@dataclass(frozen=True)
class KeySet:
    kid: str
    private_key: ec.EllipticCurvePrivateKey
    public_keys: dict[str, ec.EllipticCurvePublicKey]


def generate_private_key_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _load_keyset(settings: Settings) -> KeySet:
    if settings.jwt_private_key_pem is None:
        structlog.get_logger().warning("jwt_key_generated_ephemeral", reason="JWT_PRIVATE_KEY_PEM not set")
        pem = generate_private_key_pem()
    else:
        pem = settings.jwt_private_key_pem.get_secret_value().replace("\\n", "\n")
    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("JWT_PRIVATE_KEY_PEM must be an EC P-256 private key")
    public_keys: dict[str, ec.EllipticCurvePublicKey] = {settings.jwt_key_id: private_key.public_key()}
    if settings.jwt_previous_public_keys:
        previous = json.loads(settings.jwt_previous_public_keys.get_secret_value())
        for kid, pub_pem in previous.items():
            pub = serialization.load_pem_public_key(pub_pem.replace("\\n", "\n").encode())
            if isinstance(pub, ec.EllipticCurvePublicKey):
                public_keys[kid] = pub
    return KeySet(kid=settings.jwt_key_id, private_key=private_key, public_keys=public_keys)


@lru_cache
def get_keyset() -> KeySet:
    return _load_keyset(get_settings())


def encode_token(claims: dict[str, Any], ttl_seconds: int, token_type: str = "access") -> str:
    settings = get_settings()
    keyset = get_keyset()
    now = int(time.time())
    payload = {
        **claims,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
        "typ": token_type,
    }
    return jwt.encode(payload, keyset.private_key, algorithm=ALGORITHM, headers={"kid": keyset.kid})


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    settings = get_settings()
    keyset = get_keyset()
    try:
        header = jwt.get_unverified_header(token)
        key = keyset.public_keys.get(str(header.get("kid")))
        if key is None or header.get("alg") != ALGORITHM:
            raise Unauthorized("Invalid token.", code="invalid_token")
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Token has expired.", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid token.", code="invalid_token") from exc
    if claims.get("typ") != expected_type:
        raise Unauthorized("Invalid token type.", code="invalid_token")
    return claims
