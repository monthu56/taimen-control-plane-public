"""``X-Run-Id`` validation and propagation (ADR-0039).

The header is a trace correlator, never an authorization input: the middleware
accepts a client value only if it matches a strict shape, and otherwise mints
its own. It also has to stay distinct from the execution ``Run`` entity — the
context variable and the log field are named ``run_id``/``trace_run_id``, and
nothing in the domain reads them.
"""

from typing import Any

import pytest

from control_plane.api.middleware import RequestIdMiddleware
from control_plane.logging import JsonFormatter, request_id_var, trace_run_id_var


async def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    """Drive the middleware over one fake HTTP request; return the scope state."""
    captured: dict[str, Any] = {}
    sent: list[dict[str, Any]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured.update(scope["state"])
        captured["contextvar"] = trace_run_id_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive() -> dict[str, Any]:  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "headers": headers, "path": "/"}
    await RequestIdMiddleware(app)(scope, receive, send)
    captured["response_headers"] = dict(sent[0]["headers"])
    return captured


async def test_valid_client_run_id_is_accepted_and_echoed() -> None:
    state = await _run_middleware([(b"x-run-id", b"svc-a:req.42_x-1")])
    assert state["trace_run_id"] == "svc-a:req.42_x-1"
    assert state["contextvar"] == "svc-a:req.42_x-1"
    assert state["response_headers"][b"x-run-id"] == b"svc-a:req.42_x-1"


@pytest.mark.parametrize(
    "value",
    [
        b"",  # empty
        b"has spaces",
        b"new\nline",  # log injection attempt
        b"semi;colon",
        b"quote'",
        b"\xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82",  # non-ASCII
        b"x" * 129,  # too long
    ],
)
async def test_malformed_client_run_id_is_replaced_not_rejected(value: bytes) -> None:
    """A bad value never fails the request: it is simply not trusted."""
    state = await _run_middleware([(b"x-run-id", value)])
    assert state["trace_run_id"].startswith("run_")
    assert state["trace_run_id"] != value.decode("latin-1")


async def test_missing_run_id_is_generated_per_request() -> None:
    first = await _run_middleware([])
    second = await _run_middleware([])
    assert first["trace_run_id"].startswith("run_")
    assert first["trace_run_id"] != second["trace_run_id"]


async def test_boundary_length_is_accepted() -> None:
    value = b"a" * 128
    state = await _run_middleware([(b"x-run-id", value)])
    assert state["trace_run_id"] == "a" * 128


async def test_context_vars_are_reset_after_the_request() -> None:
    await _run_middleware([(b"x-run-id", b"trace-1")])
    assert trace_run_id_var.get() is None
    assert request_id_var.get() is None


def test_log_records_carry_run_id_separately_from_request_id() -> None:
    import logging

    token_request = request_id_var.set("req_1")
    token_run = trace_run_id_var.set("run_1")
    try:
        record = logging.LogRecord("t", logging.INFO, "f", 1, "hello", (), None)
        payload = JsonFormatter().format(record)
    finally:
        trace_run_id_var.reset(token_run)
        request_id_var.reset(token_request)
    assert '"request_id": "req_1"' in payload
    assert '"run_id": "run_1"' in payload
