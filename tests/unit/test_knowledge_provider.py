"""Knowledge proxy (CP-ADR-0060): wire shape, error mapping, authorization target.

No network and no database: the HTTP provider gets an ``httpx.MockTransport``,
and the authorization check is asked before any database access.
"""

from __future__ import annotations

import dataclasses
import json
import uuid

import httpx
import pytest

from control_plane.application.authorization import (
    Authorizer,
    configure_authorizer,
    get_authorizer,
)
from control_plane.application.commands import knowledge
from control_plane.config import Settings
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    UpstreamError,
    ValidationError,
)
from control_plane.infrastructure.context_provider import ContextProviderError, workspace_namespace
from control_plane.infrastructure.context_provider.http import HttpContextProvider
from tests.unit.test_authorizer import FakePolicy, make_ctx


def _provider(status: int, body: dict) -> tuple[list[httpx.Request], HttpContextProvider]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    return seen, provider


@pytest.mark.anyio
async def test_reconcile_wire_shape() -> None:
    seen, provider = _provider(200, {"duplicate": True})
    answer = await provider.reconcile_snapshot(
        namespace="tenant:t:ws:r",
        scopes=["workspace:w"],
        snapshot={"source": "git:x", "scope": "repo", "snapshotId": "s1", "entities": []},
        trace_run_id="run_1",
    )
    await provider.aclose()
    assert answer == {"duplicate": True}
    [request] = seen
    assert request.method == "POST"
    assert request.url.path == "/api/memory/reconcile"
    assert request.headers["Authorization"] == "Bearer k"
    assert request.headers["X-Run-Id"] == "run_1"
    # Memory's ReconcileIn: the snapshot document is flat, its ``scope`` is the
    # snapshot's own string; namespace and visibility scopes sit beside it.
    assert json.loads(request.content) == {
        "source": "git:x",
        "scope": "repo",
        "snapshotId": "s1",
        "entities": [],
        "namespace": "tenant:t:ws:r",
        "scopes": ["workspace:w"],
    }


@pytest.mark.anyio
async def test_package_and_kinds_wire_shape() -> None:
    seen, provider = _provider(200, {"ok": True})
    await provider.register_package(package={"name": "selfdev"})
    await provider.set_namespace_kinds(namespace="tenant:t:ws:r", packages=["a@1"], strict=True)
    await provider.aclose()
    package, kinds = seen
    assert (package.method, package.url.path) == ("POST", "/api/memory/packages")
    assert json.loads(package.content) == {"name": "selfdev"}
    assert kinds.method == "PUT"
    assert kinds.url.raw_path == b"/api/memory/namespaces/tenant%3At%3Aws%3Ar/kinds"
    assert json.loads(kinds.content) == {"packages": ["a@1"], "strict": True}


@pytest.mark.anyio
async def test_memory_status_is_kept_on_errors() -> None:
    _, provider = _provider(409, {"error": "stale"})
    with pytest.raises(ContextProviderError) as info:
        await provider.reconcile_snapshot(namespace="n", scopes=[], snapshot={})
    await provider.aclose()
    assert info.value.status == 409
    assert not info.value.retryable


def test_memory_failure_mapping() -> None:
    def fail(status: int | None, *, retryable: bool = False, **kwargs: object) -> Exception:
        error = ContextProviderError(
            "memory text: internal detail", retryable=retryable, status=status
        )
        return knowledge.memory_failure(error, **kwargs)  # type: ignore[arg-type]

    snapshot = {"invalid": {400: "snapshot_invalid"}, "conflict_code": "snapshot_stale"}
    invalid = fail(400, **snapshot)
    assert isinstance(invalid, ValidationError) and invalid.code == "snapshot_invalid"
    stale = fail(409, **snapshot)
    assert isinstance(stale, ConflictError) and stale.code == "snapshot_stale"
    pack = fail(409, invalid={400: "pack_invalid"}, conflict_code="pack_version_conflict")
    assert isinstance(pack, ConflictError) and pack.code == "pack_version_conflict"
    # Without a conflict meaning, a 409 is just a failed Memory call.
    other = fail(409)
    assert isinstance(other, UpstreamError) and other.code == "memory_unavailable"
    # Memory refusing the core's credential: 502, and not worth repeating.
    forbidden = fail(403, retryable=True, **snapshot)
    assert isinstance(forbidden, UpstreamError)
    assert forbidden.details == {"memoryStatus": 403, "retryable": False}
    down = fail(503, retryable=True)
    assert isinstance(down, UpstreamError) and down.details["retryable"] is True
    transport = fail(None, retryable=True)
    assert isinstance(transport, UpstreamError) and transport.details["retryable"] is True
    # Memory's text is logged, never handed to the client.
    for error in (invalid, stale, pack, other, forbidden, down, transport):
        assert "memory text" not in json.dumps(error.details)  # type: ignore[attr-defined]


def test_pack_registration_needs_a_platform_admin() -> None:
    ctx = make_ctx("admin")
    closed = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d")  # type: ignore[call-arg]
    with pytest.raises(AuthorizationError):
        knowledge.authorize_pack_registration(ctx, closed)
    other = closed.model_copy(update={"knowledge_pack_admins": [str(uuid.uuid4())]})
    with pytest.raises(AuthorizationError):
        knowledge.authorize_pack_registration(ctx, other)
    by_cp_id = closed.model_copy(update={"knowledge_pack_admins": [str(ctx.principal_id)]})
    knowledge.authorize_pack_registration(ctx, by_cp_id)
    iam_id = uuid.uuid4()
    iam_ctx = dataclasses.replace(ctx, iam_principal_id=iam_id)
    by_iam_id = closed.model_copy(update={"knowledge_pack_admins": [f" {iam_id} "]})
    knowledge.authorize_pack_registration(iam_ctx, by_iam_id)


def test_pack_references_must_be_pinned() -> None:
    assert knowledge.require_pinned_packs(["a@1", " a@1", "b-c.d@2.0-rc1"]) == [
        "a@1",
        "b-c.d@2.0-rc1",
    ]
    for unpinned in (["a"], ["a@"], ["@1"], ["a@1@2"], ["A@1"], ["a@1", "b"]):
        with pytest.raises(ValidationError) as info:
            knowledge.require_pinned_packs(unpinned)
        assert info.value.code == "pack_version_required"


def test_counters_are_numbers_only() -> None:
    counters = knowledge._counters(
        {
            "duplicate": False,
            "snapshotId": "s1",
            "accepted": 3,
            "entities": {"created": 1, "note": "text"},
            "items": [1, 2],
        }
    )
    assert counters == {"accepted": 3, "entities.created": 1}


def test_workspace_namespace_follows_policy_naming() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d")  # type: ignore[call-arg]
    tenant, root = uuid.uuid4(), uuid.uuid4()
    assert workspace_namespace(settings, tenant, root) == f"tenant:{tenant}:ws:{root}"


@pytest.mark.anyio
async def test_snapshot_is_authorized_on_the_workspace_resource() -> None:
    previous = get_authorizer()
    policy = FakePolicy(allowed=False)
    configure_authorizer(Authorizer(policy, "policy"))
    workspace_id = uuid.uuid4()
    try:
        with pytest.raises(AuthorizationError):
            await knowledge.prepare_snapshot(
                None,  # type: ignore[arg-type]
                make_ctx("observations.write"),
                Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d"),  # type: ignore[call-arg]
                workspace_id=workspace_id,
            )
    finally:
        configure_authorizer(previous)
    assert policy.calls[0][:2] == ("observations.write", f"workspace:{workspace_id}")
