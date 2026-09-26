"""Thin client for the OpenCode server's documented HTTP API (ADR-0041).

The contract implemented here is exactly what the official documentation
describes for ``opencode serve`` — nothing is inferred:

* ``GET  /global/health``      -> ``{"healthy": true, "version": "..."}``
* ``POST /session``            -> body ``{"parentID"?, "title"?}``, returns the session
* ``GET  /session/{id}``       -> one session
* ``POST /session/{id}/message`` -> body ``{"messageID"?, "model"?, "agent"?,
  "system"?, "tools"?, "parts": [...]}``, returns ``{"info", "parts"}``
* ``GET  /session/{id}/message`` -> the session's messages
* optional HTTP basic auth (username ``opencode``, password from
  ``OPENCODE_SERVER_PASSWORD``)

The official ``opencode-ai`` Python SDK is deliberately NOT a dependency: it
is a pre-release package, while these HTTP paths are the stable public
surface. Responses are parsed defensively — an added field must not break the
adapter.
"""

from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_USERNAME = "opencode"


class OpenCodeError(RuntimeError):
    """The OpenCode server was unreachable or rejected the request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenCodeClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        password: str | None = None,
        username: str = DEFAULT_USERNAME,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        auth = httpx.BasicAuth(username, password) if password else None
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            auth=auth,
            transport=transport,
            headers={"User-Agent": "control-plane-opencode/0.5"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OpenCodeClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> Any:
        try:
            response = await self._http.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"opencode server unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise OpenCodeError(
                f"opencode server rejected {method} {path} ({response.status_code}): "
                f"{response.text[:300]}",
                status=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeError(f"opencode server returned non-JSON for {path}") from exc

    async def health(self) -> dict[str, Any]:
        body = await self._request("GET", "/global/health")
        return body if isinstance(body, dict) else {}

    async def create_session(
        self, *, title: str | None = None, parent_id: str | None = None
    ) -> str:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if parent_id is not None:
            body["parentID"] = parent_id
        session = await self._request("POST", "/session", json_body=body)
        session_id = _pick_id(session)
        if session_id is None:
            raise OpenCodeError("opencode session response carried no id")
        return session_id

    async def session_exists(self, session_id: str) -> bool:
        try:
            await self._request("GET", f"/session/{session_id}")
        except OpenCodeError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Send one prompt and wait for the assistant's reply."""
        body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        if system is not None:
            body["system"] = system
        if model is not None:
            body["model"] = model
        if agent is not None:
            body["agent"] = agent
        reply = await self._request("POST", f"/session/{session_id}/message", json_body=body)
        return reply if isinstance(reply, dict) else {}

    async def abort(self, session_id: str) -> None:
        """Stop what the session is doing (the daemon stopped the run)."""
        await self._request("POST", f"/session/{session_id}/abort")

    async def list_messages(self, session_id: str) -> list[Any]:
        body = await self._request("GET", f"/session/{session_id}/message")
        return body if isinstance(body, list) else []


def _pick_id(payload: Any) -> str | None:
    """OpenCode returns ``id``; accept ``sessionID`` too rather than crashing."""
    if not isinstance(payload, dict):
        return None
    for key in ("id", "sessionID", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    info = payload.get("info")
    return _pick_id(info) if isinstance(info, dict) else None


def reply_text(reply: dict[str, Any]) -> str:
    """Concatenate the text parts of a message response, defensively."""
    parts = reply.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunk for chunk in chunks if chunk)


def reply_message_id(reply: dict[str, Any]) -> str | None:
    info = reply.get("info")
    if isinstance(info, dict):
        return _pick_id(info)
    return _pick_id(reply)
