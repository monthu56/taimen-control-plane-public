"""``scripts/process_replay_all.py`` without a core: the credential it takes and how it fails.

The walk itself runs against Postgres (tests/integration/test_process_replay_gate.py);
here the credential is the canonical client's (``resolve_credential``): an IAM
access token renewed once on a 401, an API key (the break-glass key) as it is.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from scripts import process_replay_all as script
from scripts.process_replay_all import CredentialAuth, exit_code, replay_tenant

from control_plane_client.credentials import StaticCredential
from control_plane_client.errors import ControlPlaneError

EMPTY_PAGE: dict[str, Any] = {"items": [], "nextCursor": None}


class Rotating:
    """An IAM-like credential: every refresh gives a new token."""

    refreshable = True

    def __init__(self) -> None:
        self.issued = 1
        self.refreshes = 0

    async def token(self) -> str:
        return f"token-{self.issued}"

    async def refresh(self) -> str:
        self.refreshes += 1
        self.issued += 1
        return await self.token()


def _core(accept: Callable[[str], bool], seen: list[str]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization", "")
        seen.append(bearer)
        if not accept(bearer):
            return httpx.Response(401, json={"error": {"code": "unauthenticated"}})
        return httpx.Response(200, json=EMPTY_PAGE)

    return httpx.MockTransport(handle)


async def test_an_expired_access_token_is_exchanged_again_once() -> None:
    credential = Rotating()
    seen: list[str] = []
    transport = _core(lambda bearer: bearer == "Bearer token-2", seen)
    async with httpx.AsyncClient(
        base_url="http://core", transport=transport, auth=CredentialAuth(credential)
    ) as client:
        report = await replay_tenant(client, {})
    assert report.ok
    assert (seen, credential.refreshes) == (["Bearer token-1", "Bearer token-2"], 1)


async def test_a_second_refusal_is_reported_not_retried() -> None:
    credential = Rotating()
    seen: list[str] = []
    async with httpx.AsyncClient(
        base_url="http://core",
        transport=_core(lambda _: False, seen),
        auth=CredentialAuth(credential),
    ) as client:
        report = await replay_tenant(client, {})
    assert exit_code(report) == 2
    assert [e["status"] for e in report.errors] == [401]
    assert (len(seen), credential.refreshes) == (2, 1)


async def test_an_api_key_is_sent_as_it_is_and_not_renewed() -> None:
    seen: list[str] = []
    async with httpx.AsyncClient(
        base_url="http://core",
        transport=_core(lambda _: False, seen),
        auth=CredentialAuth(StaticCredential("cp_bgkey_secret")),
    ) as client:
        report = await replay_tenant(client, {})
    assert seen == ["Bearer cp_bgkey_secret"]
    assert "cp_bgkey_secret" not in str(report.out())


def test_no_credential_is_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script, "resolve_credential", lambda _: None)
    assert script.main(["--base-url", "http://core"]) == 2
    assert "CONTROL_PLANE_IAM_URL" in capsys.readouterr().err


def test_a_broken_iam_configuration_is_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_: str) -> None:
        raise ControlPlaneError("iam_tenant_required", "CONTROL_PLANE_IAM_TENANT is not set")

    monkeypatch.setattr(script, "resolve_credential", broken)
    assert script.main(["--base-url", "http://core"]) == 2
    assert "iam_tenant_required" in capsys.readouterr().err


def test_an_exchange_iam_refuses_is_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Refused(Rotating):
        async def token(self) -> str:
            raise ControlPlaneError("iam_invalid_token", "IAM rejected the Platform Access Token")

    monkeypatch.setattr(script, "resolve_credential", lambda _: Refused())
    assert script.main(["--base-url", "http://core"]) == 2
    assert "IAM rejected" in capsys.readouterr().err
