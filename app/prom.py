"""Prometheus metrics, served on their own port and off unless PROMETHEUS_PORT is set.

A separate port keeps the scrape away from the host check, rate limits and the public
/metrics route (the offline eval results). Only the Kubernetes deployment turns it on.
"""

from __future__ import annotations

import os
import time

from prometheus_client import Counter, Histogram, start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUESTS = Counter("clearpath_http_requests_total", "HTTP requests handled", ["method", "route", "status"])
LATENCY = Histogram("clearpath_http_request_duration_seconds", "HTTP request latency", ["route"],
                    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))


def port() -> int | None:
    value = os.getenv("PROMETHEUS_PORT", "").strip()
    return int(value) if value else None


def start() -> None:
    if (p := port()) is not None:
        start_http_server(p)


class PrometheusMiddleware:
    """Counts requests and times them, labelled by route template so ids never become labels."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        start_at = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", None) or ("/mcp" if scope["path"].startswith("/mcp") else "other")
            REQUESTS.labels(scope["method"], route, str(status["code"])).inc()
            LATENCY.labels(route).observe(time.perf_counter() - start_at)
