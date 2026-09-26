"""OpenCode adapter contract: exactly the documented HTTP surface (ADR-0041).

The mandatory half runs against a local ASGI stand that implements the paths
and shapes the official ``opencode serve`` documentation describes, and asserts
what the adapter SENDS and how it parses what comes back. The optional half
runs the same assertions against a real server when ``CP_TEST_OPENCODE_URL``
is set — the binary is not part of the test image, so it is not required.
"""

import os
from typing import Any

import httpx
import pytest

from control_plane_opencode.opencode import (
    OpenCodeClient,
    OpenCodeError,
    reply_message_id,
    reply_text,
)

LIVE_URL = os.environ.get("CP_TEST_OPENCODE_URL")


class OpenCodeStand:
    """Minimal stand-in for ``opencode serve``, recording every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self._counter = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            import json

            body = json.loads(request.content)
        path = request.url.path
        self.requests.append((request.method, path, body))

        if request.method == "GET" and path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "0.0.0-stand"})
        if request.method == "POST" and path == "/session":
            self._counter += 1
            session_id = f"ses_{self._counter}"
            self.sessions[session_id] = []
            return httpx.Response(
                200, json={"id": session_id, "title": (body or {}).get("title", "")}
            )
        if request.method == "GET" and path.startswith("/session/"):
            parts = path.strip("/").split("/")
            session_id = parts[1]
            if session_id not in self.sessions:
                return httpx.Response(404, json={"error": "not found"})
            if len(parts) == 2:
                return httpx.Response(200, json={"id": session_id})
            return httpx.Response(200, json=self.sessions[session_id])
        if request.method == "POST" and path.endswith("/message"):
            session_id = path.strip("/").split("/")[1]
            if session_id not in self.sessions:
                return httpx.Response(404, json={"error": "not found"})
            message = {
                "info": {"id": f"msg_{len(self.sessions[session_id]) + 1}", "role": "assistant"},
                # A real reply mixes part kinds; the adapter must take the text.
                "parts": [
                    {"type": "step-start"},
                    {"type": "text", "text": "done: " + (body or {})["parts"][0]["text"][:20]},
                ],
            }
            self.sessions[session_id].append(message)
            return httpx.Response(200, json=message)
        return httpx.Response(404, json={"error": "unhandled"})


@pytest.fixture
def stand() -> OpenCodeStand:
    return OpenCodeStand()


@pytest.fixture
async def opencode(stand: OpenCodeStand):
    client = OpenCodeClient("http://opencode.test", transport=httpx.MockTransport(stand))
    yield client
    await client.aclose()


async def test_health_uses_the_documented_path(opencode, stand: OpenCodeStand) -> None:
    body = await opencode.health()
    assert body["healthy"] is True
    assert ("GET", "/global/health", None) in stand.requests


async def test_session_creation_posts_to_session(opencode, stand: OpenCodeStand) -> None:
    session_id = await opencode.create_session(title="TASK-000001")
    assert session_id == "ses_1"
    method, _path, body = next(r for r in stand.requests if r[1] == "/session")
    assert method == "POST"
    assert body == {"title": "TASK-000001"}


async def test_prompt_body_matches_the_documented_shape(opencode, stand: OpenCodeStand) -> None:
    session_id = await opencode.create_session()
    reply = await opencode.send_prompt(
        session_id, "Do the work", system="You are executing", model="anthropic/x", agent="build"
    )
    method, path, body = stand.requests[-1]
    assert (method, path) == ("POST", f"/session/{session_id}/message")
    assert body["parts"] == [{"type": "text", "text": "Do the work"}]
    assert body["system"] == "You are executing"
    assert body["model"] == "anthropic/x"
    assert body["agent"] == "build"
    # Optional fields are omitted rather than sent as null.
    assert set(body) == {"parts", "system", "model", "agent"}

    assert reply_text(reply).startswith("done: ")
    assert reply_message_id(reply) == "msg_1"


async def test_prompt_omits_optional_fields_when_unset(opencode, stand: OpenCodeStand) -> None:
    session_id = await opencode.create_session()
    await opencode.send_prompt(session_id, "Just text")
    _method, _path, body = stand.requests[-1]
    assert set(body) == {"parts"}


async def test_session_existence_probe_distinguishes_404(opencode) -> None:
    session_id = await opencode.create_session()
    assert await opencode.session_exists(session_id) is True
    assert await opencode.session_exists("ses_missing") is False


async def test_messages_are_listed_from_the_documented_path(opencode, stand: OpenCodeStand) -> None:
    session_id = await opencode.create_session()
    await opencode.send_prompt(session_id, "first")
    messages = await opencode.list_messages(session_id)
    assert len(messages) == 1
    assert ("GET", f"/session/{session_id}/message", None) in stand.requests


async def test_unreachable_server_is_a_typed_error() -> None:
    async def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = OpenCodeClient("http://opencode.test", transport=httpx.MockTransport(boom))
    try:
        with pytest.raises(OpenCodeError, match="unreachable"):
            await client.health()
    finally:
        await client.aclose()


async def test_server_error_carries_the_status() -> None:
    async def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = OpenCodeClient("http://opencode.test", transport=httpx.MockTransport(failing))
    try:
        with pytest.raises(OpenCodeError) as excinfo:
            await client.create_session()
        assert excinfo.value.status == 500
    finally:
        await client.aclose()


def test_reply_parsing_survives_unknown_shapes() -> None:
    """A schema addition on the OpenCode side must not break the adapter."""
    assert reply_text({}) == ""
    assert reply_text({"parts": "not-a-list"}) == ""
    assert reply_text({"parts": [{"type": "tool", "tool": "bash"}]}) == ""
    two_parts = {"parts": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert reply_text(two_parts) == "a\nb"
    assert reply_message_id({"id": "msg_9"}) == "msg_9"
    assert reply_message_id({"info": {"id": "msg_8"}}) == "msg_8"
    assert reply_message_id({}) is None


@pytest.mark.skipif(not LIVE_URL, reason="CP_TEST_OPENCODE_URL not set (real opencode serve)")
async def test_live_server_speaks_the_same_contract() -> None:
    client = OpenCodeClient(
        LIVE_URL or "", password=os.environ.get("OPENCODE_SERVER_PASSWORD") or None
    )
    try:
        health = await client.health()
        assert health.get("healthy") is True
        session_id = await client.create_session(title="control-plane contract test")
        assert await client.session_exists(session_id) is True
    finally:
        await client.aclose()
