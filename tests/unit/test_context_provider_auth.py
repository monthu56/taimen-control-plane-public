"""Credential of the HTTP Context Memory provider: static key vs IAM service account.

No network: the provider gets an ``httpx.MockTransport`` and a fake token provider,
so the tests pin what is presented on the wire and how credential failures are
classified (retryable — the adapter parks, never skips a delivery unit).
"""

from __future__ import annotations

import json

import httpx
import pytest

from control_plane.config import Settings
from control_plane.infrastructure.context_provider import (
    build_context_provider,
    resolve_context_auth,
)
from control_plane.infrastructure.context_provider.base import ContextProviderError
from control_plane.infrastructure.context_provider.http import HttpContextProvider

pytestmark = pytest.mark.unit


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+psycopg://x:y@localhost/z",
        "context_provider": "http",
        "context_api_key": "static-key",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_auto_prefers_service_account_and_falls_back_to_key() -> None:
    assert resolve_context_auth(_settings()) == "api_key"
    assert resolve_context_auth(_settings(iam_client_id="iam_sa_x", iam_client_secret="s")) == "iam"
    assert (
        resolve_context_auth(
            _settings(context_auth="api_key", iam_client_id="iam_sa_x", iam_client_secret="s")
        )
        == "api_key"
    )
    with pytest.raises(ValueError, match="CP_IAM_CLIENT_ID"):
        resolve_context_auth(_settings(context_auth="iam"))
    with pytest.raises(ValueError, match="CP_CONTEXT_AUTH"):
        resolve_context_auth(_settings(context_auth="magic"))


@pytest.mark.anyio
async def test_build_provider_in_iam_mode_uses_token_provider() -> None:
    provider = build_context_provider(
        _settings(iam_client_id="iam_sa_x", iam_client_secret="s", context_auth="iam")
    )
    assert isinstance(provider, HttpContextProvider)
    assert provider._token_provider is not None
    assert "Authorization" not in provider._http.headers
    await provider.aclose()


def _capture(status: int, body: dict) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return seen, httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_token_provider_is_asked_per_request_and_forgotten_on_401() -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self) -> None:
            self.forgotten = 0

        async def __call__(self) -> str:
            calls.append("token")
            return f"tok-{len(calls)}"

        def forget(self) -> None:
            self.forgotten += 1

    fake = FakeProvider()
    seen, transport = _capture(207, {"results": [], "accepted": 0, "duplicates": 0, "failed": 0})
    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="ignored-when-provider-set",
        token_provider=fake,
        timeout_seconds=1.0,
        ingest_timeout_seconds=1.0,
        transport=transport,
    )
    await provider.ingest_batch(namespace="tenant:t", observations=[], trace_run_id="r1")
    await provider.ingest_batch(namespace="tenant:t", observations=[])
    assert [r.headers["Authorization"] for r in seen] == ["Bearer tok-1", "Bearer tok-2"]
    assert seen[0].headers["X-Run-Id"] == "r1" and "X-Run-Id" not in seen[1].headers
    assert fake.forgotten == 0

    _, denied = _capture(401, {"detail": "expired"})
    provider._http = httpx.AsyncClient(base_url="http://memory", transport=denied)
    with pytest.raises(ContextProviderError) as exc:
        await provider.ingest_batch(namespace="tenant:t", observations=[])
    assert exc.value.retryable and exc.value.status == 401
    assert fake.forgotten == 1

    # A 403 is a valid token lacking a right: the shared token is kept.
    _, refused = _capture(403, {"detail": "service scope required"})
    provider._http = httpx.AsyncClient(base_url="http://memory", transport=refused)
    with pytest.raises(ContextProviderError) as exc:
        await provider.register_package(package={"name": "p", "version": "1"})
    assert exc.value.status == 403
    assert fake.forgotten == 1
    await provider.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("authz_mode", "extra"), [("local", ()), ("policy", ("memory:on-behalf",))]
)
async def test_core_token_carries_the_memory_service_scope(
    authz_mode: str, extra: tuple[str, ...]
) -> None:
    # One token per audience (ADR-0013): the core's identity on Memory's service
    # routes (packages, namespace kinds, reconcile) is ``memory:service``.
    provider = build_context_provider(
        _settings(
            iam_client_id="iam_sa_x",
            iam_client_secret="s",
            context_auth="iam",
            authz_mode=authz_mode,
        )
    )
    assert isinstance(provider, HttpContextProvider)
    credentials = provider._token_provider._credentials  # type: ignore[union-attr]
    assert credentials.audience == "memory-service"
    assert credentials.scopes == (
        "memory:read",
        "memory:write",
        "memory:tenants",
        "memory:service",
        *extra,
    )
    await provider.aclose()


@pytest.mark.anyio
async def test_static_key_stays_a_header_and_credential_outage_is_retryable() -> None:
    seen, transport = _capture(200, {"sections": []})
    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="static-key",
        timeout_seconds=1.0,
        ingest_timeout_seconds=1.0,
        transport=transport,
    )
    await provider.build_context(namespace="tenant:t", request={"task": "x"})
    assert seen[0].headers["Authorization"] == "Bearer static-key"
    await provider.aclose()

    async def broken() -> str:
        raise RuntimeError("service_token_exchange_failed")

    failing = HttpContextProvider(
        base_url="http://memory",
        api_key=None,
        token_provider=broken,
        timeout_seconds=1.0,
        ingest_timeout_seconds=1.0,
        transport=transport,
    )
    with pytest.raises(ContextProviderError) as exc:
        await failing.build_context(namespace="tenant:t", request={"task": "x"})
    assert exc.value.retryable and exc.value.status is None
    assert len(seen) == 1  # nothing was sent without a credential
    await failing.aclose()


@pytest.mark.anyio
async def test_read_scope_carries_every_namespace_only_when_there_are_several() -> None:
    seen, transport = _capture(200, {"sections": []})
    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="static-key",
        timeout_seconds=1.0,
        ingest_timeout_seconds=1.0,
        transport=transport,
    )
    await provider.build_context(namespace="tenant:t", request={"query": "q"})
    await provider.build_context(
        namespace="tenant:t", request={"query": "q"}, namespaces=["tenant:t"]
    )
    await provider.build_context(
        namespace="tenant:t", request={"query": "q"}, namespaces=["tenant:t", "tenant:t:ws:w"]
    )
    await provider.aclose()
    scopes = [json.loads(r.content)["scope"] for r in seen]
    assert scopes[0] == {"namespace": "tenant:t"}
    assert scopes[1] == {"namespace": "tenant:t"}
    assert scopes[2] == {"namespace": "tenant:t", "namespaces": ["tenant:t", "tenant:t:ws:w"]}
