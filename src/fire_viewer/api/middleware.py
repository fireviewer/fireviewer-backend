from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable

from prometheus_client import Counter, Histogram
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fire_viewer.core.context import trace_id_var
from fire_viewer.core.ids import TRACE_ID_RE, new_trace_id

logger = logging.getLogger("fire_viewer.http")

HTTP_REQUESTS = Counter(
    "fire_viewer_http_requests_total",
    "HTTP requests processed by the API",
    labelnames=("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "fire_viewer_http_request_duration_seconds",
    "HTTP request duration",
    labelnames=("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                await self._reject(scope, send, self.max_body_bytes)
                return
            if declared_size > self.max_body_bytes:
                await self._reject(scope, send, self.max_body_bytes)
                return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    await self._reject(scope, send, self.max_body_bytes)
                    return
                if not message.get("more_body", False):
                    break

        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(messages):
                result = messages[index]
                index += 1
                return result
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, send: Send, limit: int) -> None:
        trace_id = trace_id_var.get() or new_trace_id()
        body = json.dumps(
            {
                "type": "urn:fire-viewer:error:payload_too_large",
                "title": "Payload too large",
                "status": 413,
                "detail": f"Request body exceeds {limit} bytes.",
                "instance": scope.get("path", ""),
                "trace_id": trace_id,
            },
            separators=(",", ":"),
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-trace-id", trace_id.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get("x-trace-id", "")
        trace_id = incoming if TRACE_ID_RE.fullmatch(incoming) else new_trace_id()
        request.state.trace_id = trace_id
        token = trace_id_var.set(trace_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            duration = time.perf_counter() - started
            route = getattr(request.scope.get("route"), "path", "unmatched")
            HTTP_REQUESTS.labels(request.method, route, str(status_code)).inc()
            HTTP_DURATION.labels(request.method, route).observe(duration)
            logger.info(
                "request_completed",
                extra={
                    "method": request.method,
                    "route": route,
                    "status": status_code,
                    "duration_ms": round(duration * 1_000, 3),
                },
            )
            trace_id_var.reset(token)


class AdminNoStoreMiddleware(BaseHTTPMiddleware):
    """Prevent private administration responses from entering browser or proxy caches."""

    def __init__(self, app: ASGIApp, *, api_prefix: str) -> None:
        super().__init__(app)
        self.admin_prefixes = (f"{api_prefix.rstrip('/')}/admin", "/api/v2/admin")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in self.admin_prefixes):
            response.headers["Cache-Control"] = "no-store"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, enable_hsts: bool) -> None:
        super().__init__(app)
        self.enable_hsts = enable_hsts

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'",
        )
        if self.enable_hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
