"""Meta webhook authentication: X-Hub-Signature-256 over the raw request body."""

from __future__ import annotations

import hashlib
import hmac


def compute_signature(body: bytes, app_secret: str) -> str:
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def is_valid_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(compute_signature(body, app_secret), header.strip())
