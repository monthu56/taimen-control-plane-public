"""Pure ASGI middleware: request-id propagation, body size limit, metrics."""

import re
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from control_plane.api.errors import BodyTooLargeError
from control_plane.logging import request_id_var, trace_run_id_var

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
# X-Run-Id is a client-supplied trace correlator (ADR-0039): validated for
# shape only, never trusted for a decision. Colons are allowed so callers can
# carry their own "service:id" convention.
_TRACE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _header(scope: Scope, name: bytes) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestIdMiddleware:
    """Accept X-Request-ID and X-Run-Id from the client (sanitized) or generate them.

    Both are echoed back on the response so a caller can correlate without
    guessing; both are shape-validated, because they end up in structured logs
    and in the durable event journal.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        supplied = _header(scope, b"x-request-id")
        request_id = (
            supplied if supplied and _REQUEST_ID_RE.match(supplied) else f"req_{uuid.uuid4().hex}"
        )
        supplied_run = _header(scope, b"x-run-id")
        trace_run_id = (
            supplied_run
            if supplied_run and _TRACE_RUN_ID_RE.match(supplied_run)
            else f"run_{uuid.uuid4().hex}"
        )
        token = request_id_var.set(request_id)
        run_token = trace_run_id_var.set(trace_run_id)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["trace_run_id"] = trace_run_id

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                headers.append((b"x-run-id", trace_run_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
            trace_run_id_var.reset(run_token)


class BodySizeLimitMiddleware:
    """Reject request bodies above the configured limit.

    A declared oversized Content-Length is rejected up front; chunked bodies
    are counted as they stream and rejected on overflow (before any response
    has started, since FastAPI reads the body before handling).
    ``path_limits`` overrides the ceiling for exact request paths.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int,
        path_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.path_limits = dict(path_limits or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.path_limits.get(scope.get("path", ""), self.max_body_bytes)
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            # Raised outside the exception-handler stack, so respond directly.
            from control_plane.api.errors import error_response

            response = error_response(
                413, "request_too_large", "Request body exceeds the size limit"
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLargeError()
            return message

        await self.app(scope, limited_receive, send)


class MetricsMiddleware:
    """Count HTTP requests by method and response class into app metrics."""

    def __init__(self, app: ASGIApp, counters: dict[str, Any]) -> None:
        self.app = app
        self.counters = counters

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Arbitrary method tokens are valid HTTP: whitelist so unauthenticated
        # junk cannot grow the counter dict (and /metrics output) unboundedly.
        method = scope.get("method", "GET")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            method = "OTHER"

        async def counting_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                key = (method, status)
                requests = self.counters.setdefault("http_requests_total", {})
                requests[key] = requests.get(key, 0) + 1
            await send(message)

        await self.app(scope, receive, counting_send)
