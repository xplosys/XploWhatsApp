"""Fetch a public web page for the knowledge base without letting it reach inside our
network (SSRF).

Every hop is resolved here, every resolved address must be public, and the request is
sent to that exact address (with the original Host header and TLS name), so DNS can't
be switched between the check and the connection. Redirects are followed by hand and
checked the same way. No proxy settings from the environment are used.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import get_settings
from app.modules.knowledge.models import FileType

USER_AGENT = "XploWhatsApp-KnowledgeFetcher/1.0 (+https://xplowhatsapp.com)"
ALLOWED_TYPES = {"text/html": FileType.HTML, "text/plain": FileType.TXT, "application/pdf": FileType.PDF}

_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


class FetchError(Exception):
    def __init__(self, message: str, code: str = "fetch_failed"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Fetched:
    url: str
    file_type: FileType
    content_type: str
    charset: str | None
    body: bytes


async def resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"The address {host} could not be found.", "dns_failed") from exc
    return sorted({str(info[4][0]) for info in infos})


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise FetchError("Only http and https links can be added.", "invalid_url")
    if not parts.hostname or parts.username or parts.password:
        raise FetchError("The link is not a valid web address.", "invalid_url")
    if parts.port not in (None, 80, 443):
        raise FetchError("Only standard web ports (80 and 443) are allowed.", "invalid_url")
    return parts.geturl()


async def _pinned(url: str) -> tuple[str, dict[str, str], dict[str, object]]:
    """The URL rewritten to a checked IP, plus the Host header and TLS name to use."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = await resolve(host, port)
    if not addresses:
        raise FetchError(f"The address {host} could not be found.", "dns_failed")
    if not get_settings().knowledge_url_allow_private and not all(is_public(a) for a in addresses):
        raise FetchError(
            "That address points to a private or local network and can't be fetched.", "blocked_address"
        )
    address = addresses[0]
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parts.port}" if parts.port else literal
    pinned = parts._replace(netloc=netloc).geturl()
    host_header = f"{host}:{parts.port}" if parts.port else host
    extensions: dict[str, object] = {"sni_hostname": host} if parts.scheme == "https" else {}
    return pinned, {"Host": host_header}, extensions


def _parse_content_type(value: str) -> tuple[str, str | None]:
    main, _, params = value.partition(";")
    charset = None
    for param in params.split(";"):
        key, _, val = param.strip().partition("=")
        if key.lower() == "charset" and val:
            charset = val.strip("\"' ")
    return main.strip().lower(), charset


async def fetch(url: str) -> Fetched:
    settings = get_settings()
    current = validate_url(url)
    max_bytes = settings.knowledge_url_max_mb * 1024 * 1024
    timeout = httpx.Timeout(settings.knowledge_url_timeout_seconds)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False, transport=_transport_override
    ) as client:
        for _ in range(settings.knowledge_url_max_redirects + 1):
            pinned, headers, extensions = await _pinned(current)
            request = client.build_request(
                "GET",
                pinned,
                headers={
                    **headers,
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,text/plain,application/pdf",
                },
                extensions=extensions,
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise FetchError("The website took too long to answer.", "timeout") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"The website could not be reached: {exc}"[:300], "unreachable") from exc
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("The website sent a redirect without a target.")
                    current = validate_url(urljoin(current, location))
                    continue
                if response.status_code != 200:
                    raise FetchError(f"The website answered with HTTP {response.status_code}.", "http_error")
                content_type, charset = _parse_content_type(response.headers.get("content-type", ""))
                file_type = ALLOWED_TYPES.get(content_type)
                if file_type is None:
                    raise FetchError(
                        f"The link returns {content_type or 'an unknown type'}; "
                        "only web pages, text and PDF are supported.",
                        "unsupported_type",
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(
                        f"The page is larger than {settings.knowledge_url_max_mb} MB.", "too_large"
                    )
                body = bytearray()
                async for part in response.aiter_bytes():
                    body += part
                    if len(body) > max_bytes:
                        raise FetchError(
                            f"The page is larger than {settings.knowledge_url_max_mb} MB.", "too_large"
                        )
                return Fetched(current, file_type, content_type, charset, bytes(body))
            finally:
                await response.aclose()
    raise FetchError("The website redirected too many times.", "too_many_redirects")
