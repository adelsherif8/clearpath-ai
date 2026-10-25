"""HTTP hardening: client-IP resolution, rate limiting, body-size limit, security headers.

All middleware here is plain ASGI so it also wraps the MCP sub-application.
"""

from __future__ import annotations

from limits import parse as parse_limit
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from slowapi import Limiter
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings


# ── client IP ────────────────────────────────────────────────────────────────
def client_ip(request: Request) -> str:
    """The real client address.

    Behind Cloud Run the Google Front End *appends* the connecting IP to
    X-Forwarded-For, so the right-most entry is the only one that cannot be
    spoofed by the client. Anywhere else we use the socket peer and ignore the
    header entirely (never trust it by default).
    """
    if settings().trust_proxy == "cloudrun":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


# ── rate limiting ────────────────────────────────────────────────────────────
_storage = MemoryStorage()
_window = MovingWindowRateLimiter(_storage)



def new_limiter() -> Limiter:
    """One limiter per app instance (slowapi keys limits by function name, so sharing one
    across app rebuilds would stack duplicate limits)."""
    return Limiter(key_func=client_ip, headers_enabled=False, storage_uri="memory://")


def hit(bucket: str, key: str, limit: str) -> bool:
    """Shared moving-window limiter for code paths slowapi's decorator can't wrap
    (the MCP tools). Returns True when the call is allowed."""
    return _window.hit(parse_limit(limit), bucket, key)


def reset_limits() -> None:
    _storage.reset()


def rate_limited_response(request: Request, exc) -> JSONResponse:
    from app import audit  # local import: audit imports nothing from here
    audit.record("rate_limited", None, ip=client_ip(request), outcome="denied",
                 meta={"path": request.url.path, "limit": str(getattr(exc, "detail", ""))})
    return JSONResponse(
        {"error": "Too many requests from this address. Please slow down and try again in a minute."},
        status_code=429, headers={"Retry-After": "60"},
    )


# ── body size limit ──────────────────────────────────────────────────────────
class BodySizeLimitMiddleware:
    """Reject request bodies larger than `max_bytes` with 413 before they are read."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send, status=400)
                return

        received = 0
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes and not rejected:
                    rejected = True
                    await self._reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not rejected:          # once we've answered 413, drop whatever the app sends
                await send(message)

        await self.app(scope, limited_receive, guarded_send)

    async def _reject(self, send: Send, status: int = 413) -> None:
        body = b'{"error":"Request body too large."}' if status == 413 else b'{"error":"Bad request."}'
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# ── security headers ─────────────────────────────────────────────────────────
def content_security_policy() -> str:
    connect = " ".join(["'self'"] + settings().extra_connect_src)
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "img-src 'self' data:; "
        f"connect-src {connect}; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; upgrade-insecure-requests"
    )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_html_app = not scope["path"].startswith("/mcp")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"x-frame-options": b"DENY",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=()",
                    b"cross-origin-opener-policy": b"same-origin",
                    b"cross-origin-resource-policy": b"same-origin",
                    b"cache-control": b"no-store",
                }
                if is_html_app:
                    extra[b"content-security-policy"] = content_security_policy().encode()
                else:
                    extra[b"content-security-policy"] = b"default-src 'none'; frame-ancestors 'none'"
                if settings().is_production:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains; preload"
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra.items() if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── MCP sub-app guard ────────────────────────────────────────────────────────
MCP_IP_HEADER = b"x-cp-client-ip"


class McpGuardMiddleware:
    """Wraps the MCP Starlette app.

    * Stamps the real client IP into a header the tools can read (any client-supplied
      copy of that header is removed first, so it can't be spoofed).
    * Applies a coarse per-IP request limit to the whole MCP endpoint; individual tools
      add their own, stricter limits for the LLM-backed calls.
    """

    def __init__(self, app: ASGIApp, limit: str | None = None) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ip = client_ip(Request(scope))
        if scope.get("method") == "POST" and not hit("mcp_http", ip, self.limit or settings().rate_limit_default):
            from app import audit
            audit.record("rate_limited", None, ip=ip, outcome="denied", meta={"path": "/mcp/"})
            body = b'{"error":"Too many requests from this address."}'
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"60"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        headers = [(k, v) for k, v in (scope.get("headers") or []) if k.lower() != MCP_IP_HEADER]
        headers.append((MCP_IP_HEADER, ip.encode("latin1", "replace")))
        await self.app({**scope, "headers": headers}, receive, send)
