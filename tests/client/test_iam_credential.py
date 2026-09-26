"""The harness credential backed by an IAM identity (IAM-7).

An IAM access token lives minutes while a harness session lives hours, so what
matters here is not that an exchange happens but that it happens again — before
expiry, and once more when the server says the token is no longer accepted.
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane_client import (
    AuthenticationError,
    ControlPlaneClient,
    IamCredential,
    IamCredentialError,
)
from control_plane_client.credentials import StaticCredential, resolve_credential
from control_plane_client.iam import iam_credential_from_environment

IAM_URL = "https://iam.test"
TENANT = "11111111-1111-1111-1111-111111111111"
ACCOUNT = f"{IAM_URL}|{TENANT}"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _iam_transport(state: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] = int(state.get("calls", 0)) + 1
        state["last_body"] = json.loads(request.content)
        if state.get("status", 200) != 200:
            return httpx.Response(state["status"], json={"detail": "denied"})
        return httpx.Response(
            200,
            json={
                "accessToken": f"token-{state['calls']}",
                "tokenType": "Bearer",
                "expiresIn": state.get("expires_in", 300),
                "audience": "control-plane",
                "scope": ["control-plane:read"],
                "sessionId": "22222222-2222-2222-2222-222222222222",
            },
        )

    return httpx.MockTransport(handler)


def _credential(
    state: dict[str, Any], clock: Clock, environ: dict[str, str] | None = None
) -> IamCredential:
    return IamCredential(
        IAM_URL,
        TENANT,
        scopes=("control-plane:read",),
        environ=environ
        or {"IAM_CREDENTIAL_MODE": "environment", "IAM_PLATFORM_ACCESS_TOKEN": "iam_pat_x_y"},
        transport=_iam_transport(state),
        clock=clock,
    )


async def test_token_is_exchanged_once_and_cached(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    clock = Clock()
    credential = _credential(state, clock)

    assert await credential.token() == "token-1"
    clock.advance(100)
    assert await credential.token() == "token-1"

    assert state["calls"] == 1


async def test_token_is_re_exchanged_before_expiry() -> None:
    """A token that expires in flight would surface as a failed command."""
    state: dict[str, Any] = {"expires_in": 300}
    clock = Clock()
    credential = _credential(state, clock)
    await credential.token()

    clock.advance(280)

    assert await credential.token() == "token-2"
    assert state["calls"] == 2


async def test_platform_access_token_goes_only_in_the_body() -> None:
    state: dict[str, Any] = {}
    credential = _credential(state, Clock())

    await credential.token()

    assert state["last_body"]["token"] == "iam_pat_x_y"
    assert state["last_body"]["audience"] == "control-plane"


async def test_rejected_platform_access_token_says_what_to_do() -> None:
    state: dict[str, Any] = {"status": 401}
    credential = _credential(state, Clock())

    with pytest.raises(IamCredentialError) as exc:
        await credential.token()

    assert exc.value.code == "iam_invalid_token"
    assert "iam auth login" in str(exc.value)


async def test_audience_denial_is_reported_separately() -> None:
    state: dict[str, Any] = {"status": 403}
    credential = _credential(state, Clock())

    with pytest.raises(IamCredentialError) as exc:
        await credential.token()

    assert exc.value.code == "iam_audience_not_allowed"


async def test_inherited_environment_variable_is_not_a_silent_credential() -> None:
    """An undeclared mode is an error, not a quiet source of someone's token."""
    credential = _credential({}, Clock(), environ={"IAM_PLATFORM_ACCESS_TOKEN": "iam_pat_x_y"})

    with pytest.raises(IamCredentialError) as exc:
        await credential.token()

    assert exc.value.code == "iam_environment_mode_required"


async def test_world_readable_credentials_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "iam" / "credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({ACCOUNT: {"token": "iam_pat_x_y"}}), encoding="utf-8")
    path.chmod(0o644)
    credential = _credential(
        {}, Clock(), environ={"XDG_CONFIG_HOME": str(tmp_path), "IAM_NO_KEYCHAIN": "1"}
    )

    with pytest.raises(IamCredentialError) as exc:
        await credential.token()

    assert exc.value.code == "iam_credentials_file_permissions"


async def test_protected_credentials_file_is_used(tmp_path: Path) -> None:
    path = tmp_path / "iam" / "credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({ACCOUNT: {"token": "iam_pat_x_y"}}), encoding="utf-8")
    path.chmod(0o600)
    state: dict[str, Any] = {}
    credential = _credential(
        state, Clock(), environ={"XDG_CONFIG_HOME": str(tmp_path), "IAM_NO_KEYCHAIN": "1"}
    )

    assert await credential.token() == "token-1"
    assert state["last_body"]["token"] == "iam_pat_x_y"


async def test_missing_login_is_reported_as_such(tmp_path: Path) -> None:
    credential = _credential(
        {}, Clock(), environ={"XDG_CONFIG_HOME": str(tmp_path), "IAM_NO_KEYCHAIN": "1"}
    )

    with pytest.raises(IamCredentialError) as exc:
        await credential.token()

    assert exc.value.code == "iam_not_authenticated"


# --- integration with the transport ------------------------------------------


def _control_plane_transport(state: dict[str, Any]) -> httpx.AsyncBaseTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        state.setdefault("requests", []).append(request)
        if state.get("reject_first") and len(state["requests"]) == 1:
            return httpx.Response(401, json={"error": {"code": "invalid_credentials"}})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


async def test_expired_token_is_renewed_and_the_command_is_not_duplicated() -> None:
    iam_state: dict[str, Any] = {}
    cp_state: dict[str, Any] = {"reject_first": True}
    credential = _credential(iam_state, Clock())
    client = ControlPlaneClient(
        "https://cp.test", credential, transport=_control_plane_transport(cp_state)
    )

    result = await client._request("POST", "/tasks", json_body={"title": "t"}, idempotent=True)

    assert result == {"ok": True}
    first, second = cp_state["requests"]
    # The retry carries a renewed token but the same key: a re-sent request
    # must remain the same business command.
    assert first.headers["Authorization"] != second.headers["Authorization"]
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert iam_state["calls"] == 2
    await client.aclose()


async def test_a_second_rejection_is_a_denial_not_a_retry_loop() -> None:
    iam_state: dict[str, Any] = {}
    cp_state: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        cp_state.setdefault("requests", []).append(request)
        return httpx.Response(401, json={"error": {"code": "invalid_credentials"}})

    credential = _credential(iam_state, Clock())
    client = ControlPlaneClient(
        "https://cp.test", credential, transport=httpx.MockTransport(handler)
    )

    with pytest.raises(AuthenticationError):
        await client._request("GET", "/tasks")

    assert len(cp_state["requests"]) == 2
    await client.aclose()


async def test_api_key_is_never_renewed() -> None:
    cp_state: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        cp_state.setdefault("requests", []).append(request)
        return httpx.Response(401, json={"error": {"code": "invalid_credentials"}})

    client = ControlPlaneClient(
        "https://cp.test", "cp_key_secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(AuthenticationError):
        await client._request("GET", "/tasks")

    # No renewal path exists for an API key, so exactly one attempt is made.
    assert len(cp_state["requests"]) == 1
    await client.aclose()


# --- resolution ---------------------------------------------------------------


def test_iam_is_not_configured_without_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTROL_PLANE_IAM_URL", raising=False)

    assert iam_credential_from_environment({}) is None


def test_configured_iam_wins_over_the_legacy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_IAM_URL", IAM_URL)
    monkeypatch.setenv("CONTROL_PLANE_IAM_TENANT", TENANT)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "cp_key_secret")

    credential = resolve_credential("https://cp.test")

    assert isinstance(credential, IamCredential)


def test_api_key_is_used_when_iam_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTROL_PLANE_IAM_URL", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "cp_key_secret")

    credential = resolve_credential("https://cp.test")

    assert isinstance(credential, StaticCredential)


def test_url_without_tenant_is_a_broken_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-configured IAM must not fall back to the old key unnoticed."""
    monkeypatch.setenv("CONTROL_PLANE_IAM_URL", IAM_URL)
    monkeypatch.delenv("CONTROL_PLANE_IAM_TENANT", raising=False)

    with pytest.raises(IamCredentialError) as exc:
        resolve_credential("https://cp.test")

    assert exc.value.code == "iam_tenant_required"
    assert os.environ.get("CONTROL_PLANE_IAM_URL") == IAM_URL


# --- several executors of one tenant on one machine ----------------------------
#
# Their records collide under the old key, and the loser keeps working — as
# somebody else. That is invisible outside audit, so it is refused here.

RUNNER = "44444444-4444-4444-4444-444444444444"
REVIEWER = "55555555-5555-5555-5555-555555555555"


def _store(tmp_path: Path, document: dict[str, Any]) -> dict[str, str]:
    path = tmp_path / "iam" / "credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return {"XDG_CONFIG_HOME": str(tmp_path), "IAM_NO_KEYCHAIN": "1"}


async def test_each_executor_gets_its_own_credential(tmp_path: Path) -> None:
    environ = _store(
        tmp_path,
        {
            f"{ACCOUNT}|{RUNNER}": {"token": "iam_pat_runner_x", "principalId": RUNNER},
            f"{ACCOUNT}|{REVIEWER}": {"token": "iam_pat_reviewer_x", "principalId": REVIEWER},
        },
    )
    state: dict[str, Any] = {}

    await _credential(state, Clock(), environ={**environ, "IAM_PRINCIPAL": REVIEWER}).token()

    assert state["last_body"]["token"] == "iam_pat_reviewer_x"


async def test_a_process_that_does_not_say_who_it_is_is_refused_not_guessed(
    tmp_path: Path,
) -> None:
    environ = _store(
        tmp_path,
        {
            f"{ACCOUNT}|{RUNNER}": {"token": "iam_pat_runner_x", "principalId": RUNNER},
            f"{ACCOUNT}|{REVIEWER}": {"token": "iam_pat_reviewer_x", "principalId": REVIEWER},
        },
    )

    with pytest.raises(IamCredentialError) as exc:
        await _credential({}, Clock(), environ=environ).token()

    assert exc.value.code == "iam_credential_ambiguous"
    assert "IAM_PRINCIPAL" in str(exc.value)


async def test_a_credential_in_the_os_store_still_counts_as_present(tmp_path: Path) -> None:
    """The keychain cannot be enumerated, so the index is what makes it visible."""
    environ = _store(
        tmp_path,
        {
            f"{ACCOUNT}|{RUNNER}": {"token": "iam_pat_runner_x", "principalId": RUNNER},
            "principals": {ACCOUNT: [REVIEWER]},
        },
    )

    with pytest.raises(IamCredentialError) as exc:
        await _credential({}, Clock(), environ=environ).token()

    assert exc.value.code == "iam_credential_ambiguous"


async def test_one_executor_needs_no_declaration(tmp_path: Path) -> None:
    environ = _store(
        tmp_path, {f"{ACCOUNT}|{RUNNER}": {"token": "iam_pat_only_x", "principalId": RUNNER}}
    )
    state: dict[str, Any] = {}

    await _credential(state, Clock(), environ=environ).token()

    assert state["last_body"]["token"] == "iam_pat_only_x"


async def test_an_entry_of_the_old_format_is_still_read(tmp_path: Path) -> None:
    """A machine of the previous release keeps working without being touched."""
    environ = _store(tmp_path, {ACCOUNT: {"token": "iam_pat_legacy_x"}})
    state: dict[str, Any] = {}

    await _credential(state, Clock(), environ=environ).token()

    assert state["last_body"]["token"] == "iam_pat_legacy_x"


async def test_someone_elses_entry_of_the_old_format_is_not_borrowed(tmp_path: Path) -> None:
    environ = _store(tmp_path, {ACCOUNT: {"token": "iam_pat_someone_x", "principalId": REVIEWER}})

    with pytest.raises(IamCredentialError) as exc:
        await _credential({}, Clock(), environ={**environ, "IAM_PRINCIPAL": RUNNER}).token()

    assert exc.value.code == "iam_not_authenticated"


# --- a PAT handed in explicitly ------------------------------------------------
#
# A process whose PAT lives in a per-process secret file (a container mount, a
# vertical package's ``<slug>.pat``) hands it in and never touches the store.


async def test_explicit_platform_access_token_skips_the_store(tmp_path: Path) -> None:
    environ = _store(
        tmp_path, {f"{ACCOUNT}|{RUNNER}": {"token": "iam_pat_store_x", "principalId": RUNNER}}
    )
    state: dict[str, Any] = {}
    credential = IamCredential(
        IAM_URL,
        TENANT,
        scopes=("control-plane:read",),
        environ=environ,
        transport=_iam_transport(state),
        clock=Clock(),
        platform_access_token="iam_pat_explicit_x",
    )

    assert await credential.token() == "token-1"
    assert state["last_body"] == {
        "token": "iam_pat_explicit_x",
        "audience": "control-plane",
        "scopes": ["control-plane:read"],
    }


async def test_explicit_provider_is_asked_on_every_exchange() -> None:
    """A callable is read per exchange, so a rotated secret file is picked up
    without a restart — and the cache still holds between exchanges."""
    pats = iter(["iam_pat_first_x", "iam_pat_second_x"])
    state: dict[str, Any] = {"expires_in": 300}
    clock = Clock()
    credential = IamCredential(
        IAM_URL,
        TENANT,
        transport=_iam_transport(state),
        clock=clock,
        platform_access_token=lambda: next(pats),
    )

    assert await credential.token() == "token-1"
    assert await credential.token() == "token-1"
    assert state["last_body"]["token"] == "iam_pat_first_x"
    clock.advance(280)
    assert await credential.token() == "token-2"
    assert state["last_body"]["token"] == "iam_pat_second_x"
    assert state["calls"] == 2


async def test_explicit_token_needs_no_tenant() -> None:
    """The tenant addresses a record in the store; an explicit PAT has none."""
    state: dict[str, Any] = {}
    credential = IamCredential(
        IAM_URL, "", transport=_iam_transport(state), platform_access_token="iam_pat_x_y"
    )

    assert await credential.token() == "token-1"
    assert credential.account == f"{IAM_URL}|"

    with pytest.raises(IamCredentialError) as exc:
        IamCredential(IAM_URL, "")
    assert exc.value.code == "iam_tenant_required"


async def test_empty_explicit_token_is_not_authenticated() -> None:
    credential = IamCredential(IAM_URL, TENANT, platform_access_token=lambda: "  ")

    with pytest.raises(IamCredentialError) as exc:
        await credential.token()

    assert exc.value.code == "iam_not_authenticated"


async def test_rejected_explicit_token_does_not_suggest_login() -> None:
    """``iam auth login`` renews the store; an explicit PAT is renewed elsewhere."""
    credential = IamCredential(
        IAM_URL,
        TENANT,
        transport=_iam_transport({"status": 401}),
        platform_access_token="iam_pat_x_y",
    )

    with pytest.raises(IamCredentialError) as exc:
        await credential.token()

    assert exc.value.code == "iam_invalid_token"
    assert "iam auth login" not in str(exc.value)


def test_audience_and_scopes_are_readable() -> None:
    credential = IamCredential(
        IAM_URL, TENANT, audience="bidops", scopes=("bidops:read",), platform_access_token="p"
    )

    assert credential.audience == "bidops"
    assert credential.scopes == ("bidops:read",)
