"""Malware scanning with clamd over TCP (INSTREAM).

Protocol (clamd man page): send `zINSTREAM\\0`, then chunks each prefixed with their
length as a 4-byte big-endian integer, then a zero-length chunk. clamd answers
`stream: OK`, `stream: <signature> FOUND` or `... ERROR`, terminated by NUL.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from app.core.config import get_settings

CHUNK = 64 * 1024


class ScannerUnavailable(Exception):
    """clamd could not be reached or answered with an error. The file is not processed."""


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    signature: str | None = None

    @property
    def summary(self) -> str:
        return "clean" if self.clean else f"infected: {self.signature}"


def scanner_configured() -> bool:
    return bool(get_settings().clamav_host)


async def _exchange(command: bytes | None, data: bytes | None) -> str:
    settings = get_settings()
    assert settings.clamav_host
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.clamav_host, settings.clamav_port), timeout=10
        )
    except (OSError, TimeoutError) as exc:
        raise ScannerUnavailable(f"Cannot reach the virus scanner: {exc}") from exc
    try:
        if command is not None:
            writer.write(command)
        if data is not None:
            for i in range(0, len(data), CHUNK):
                part = data[i : i + CHUNK]
                writer.write(struct.pack(">I", len(part)) + part)
                await writer.drain()
            writer.write(struct.pack(">I", 0))
        await writer.drain()
        reply = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=settings.clamav_timeout_seconds)
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise ScannerUnavailable(f"The virus scanner did not answer: {exc}") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return reply.rstrip(b"\0").decode("utf-8", "replace").strip()


async def ping() -> bool:
    try:
        return await _exchange(b"zPING\0", None) == "PONG"
    except ScannerUnavailable:
        return False


async def scan(data: bytes) -> ScanResult:
    reply = await _exchange(b"zINSTREAM\0", data)
    # e.g. "stream: OK" or "stream: Win.Test.EICAR_HDB-1 FOUND"
    body = reply.split(":", 1)[1].strip() if ":" in reply else reply
    if body == "OK":
        return ScanResult(True)
    if body.endswith(" FOUND"):
        return ScanResult(False, body.removesuffix(" FOUND").strip()[:150])
    raise ScannerUnavailable(f"Unexpected scanner reply: {reply[:200]}")
