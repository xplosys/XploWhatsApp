from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


# The API only returns JSON, so its pages may load nothing and be framed by nobody.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# Swagger UI (development only) loads its script and styles from a CDN.
DOCS_CSP = (
    "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID, binds it to logs, and adds the security headers."""

    def __init__(self, app: ASGIApp, hsts_max_age: int | None = None):
        super().__init__(app)
        self.hsts = f"max-age={hsts_max_age}; includeSubDomains" if hsts_max_age else None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        docs = request.url.path in ("/docs", "/docs/oauth2-redirect")
        response.headers.setdefault("Content-Security-Policy", DOCS_CSP if docs else API_CSP)
        if self.hsts:
            response.headers.setdefault("Strict-Transport-Security", self.hsts)
        structlog.get_logger("http").info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response


class UploadSizeLimitMiddleware:
    """Refuse multipart uploads that declare a body larger than allowed, before anything
    is parsed or spooled to disk. Uploads without a Content-Length are refused too.
    (The reverse proxy enforces the same limit in production.)"""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if headers.get("content-type", "").lower().startswith("multipart/"):
                length = headers.get("content-length", "")
                problem = None
                if not length.isdigit():
                    problem = (411, "length_required", "Uploads must declare their size.")
                elif int(length) > self.max_bytes:
                    mb = self.max_bytes // (1024 * 1024)
                    problem = (413, "file_too_large", f"The upload is larger than {mb} MB.")
                if problem:
                    status, code, message = problem
                    response = JSONResponse(
                        {"error": {"code": code, "message": message, "details": None, "request_id": None}},
                        status_code=status,
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
