"""Reading back what a pack install writes (CP-ADR-0060, amendment 2026-09-30).

``GET /workspaces/{id}/knowledge-packs`` reads Memory's ``GET
/api/memory/namespaces/{ns}/kinds`` and ``GET /knowledge/packs/{ref}`` its ``GET
/api/memory/packages/{name}``. The requests are checked against the pinned
routes of ``tests/fixtures/memory_graph_contract.json`` and the answers are
built from the pinned answers of Memory's own code (``namespaceKinds``,
``packages``), so the mapping is proven on Memory's shapes, not on a fake's.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from control_plane.api.v1.schemas import KnowledgePackOut, WorkspaceKnowledgePacksOut
from control_plane.application.commands import knowledge
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError, UpstreamError, ValidationError
from control_plane.infrastructure.context_provider.http import HttpContextProvider

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_graph_contract.json").read_text()
)
PACKAGE_PARAMS = CONTRACT["paths"]["/api/memory/packages/{name}"]["get"]["parameters"]
TARGET = knowledge.KnowledgeTarget(
    workspace_id=uuid.UUID(int=2),
    root_workspace_id=uuid.UUID(int=1),
    namespace=f"tenant:t-1:ws:{uuid.UUID(int=1)}",
    scopes=[f"workspace:{uuid.UUID(int=2)}"],
)


def _provider(status: int, answer: Any, seen: list[httpx.Request]) -> HttpContextProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=answer)

    return HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )


async def _workspace_packs(answer: dict[str, Any]) -> tuple[httpx.Request, dict[str, Any]]:
    seen: list[httpx.Request] = []
    provider = _provider(200, answer, seen)
    try:
        result = await knowledge.read_workspace_packs(provider, TARGET)
    finally:
        await provider.aclose()
    [request] = seen
    return request, result


async def _pack(
    read: knowledge.PackRead, status: int = 200, answer: Any = None
) -> tuple[httpx.Request, dict[str, Any]]:
    seen: list[httpx.Request] = []
    provider = _provider(status, answer if answer is not None else {}, seen)
    try:
        result = await knowledge.read_pack(provider, read)
    finally:
        await provider.aclose()
    [request] = seen
    return request, result


# --- the set of a workspace ----------------------------------------------------


@pytest.mark.anyio
async def test_workspace_packs_are_read_from_the_roots_namespace_kinds() -> None:
    request, result = await _workspace_packs(CONTRACT["namespaceKinds"])
    assert request.method == "GET" and request.content == b""
    assert request.url.path == f"/api/memory/namespaces/{TARGET.namespace}/kinds"
    # Memory's settings: the packs as PUT set them; its catalog: what applies.
    assert result == {
        "workspaceId": str(TARGET.workspace_id),
        "rootWorkspaceId": str(TARGET.root_workspace_id),
        "configured": True,
        "packs": ["software-delivery"],
        "strict": False,
        "effective": ["software-delivery@1"],
        "updatedAt": None,
    }
    WorkspaceKnowledgePacksOut.model_validate(result)
    # The namespace is the core's to know, not the client's.
    assert TARGET.namespace not in json.dumps(result)


@pytest.mark.anyio
async def test_an_unconfigured_namespace_is_not_an_empty_set() -> None:
    """Memory keeps ``packages: null`` until the first PUT: its default pack applies."""
    answer = {
        "settings": {
            "namespace": TARGET.namespace,
            "strict": False,
            "packages": None,
            "updated_at": "",
            "updated_by": "",
        },
        "catalog": {**CONTRACT["namespaceKinds"]["catalog"], "packages": ["default@1"]},
    }
    _, result = await _workspace_packs(answer)
    assert result["configured"] is False
    assert result["packs"] == []
    assert result["effective"] == ["default@1"]


@pytest.mark.anyio
async def test_a_configured_namespace_carries_its_strictness_and_time() -> None:
    answer = {
        "settings": {
            "namespace": TARGET.namespace,
            "strict": True,
            "packages": ["selfdev@1", "tenant:fleet@2"],
            "updated_at": "2026-09-30T10:00:00+00:00",
            "updated_by": "core",
        },
        "catalog": {"packages": ["selfdev@1", "tenant:fleet@2"]},
    }
    _, result = await _workspace_packs(answer)
    assert result["configured"] is True
    assert result["strict"] is True
    assert result["packs"] == ["selfdev@1", "tenant:fleet@2"]
    assert result["updatedAt"] == "2026-09-30T10:00:00+00:00"
    assert "updated_by" not in result


@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_a_memory_failure_on_the_set_is_memorys(status: int) -> None:
    seen: list[httpx.Request] = []
    provider = _provider(status, {"detail": "no"}, seen)
    try:
        with pytest.raises(UpstreamError) as info:
            await knowledge.read_workspace_packs(provider, TARGET)
    finally:
        await provider.aclose()
    assert info.value.code == "memory_unavailable"


# --- one pack --------------------------------------------------------------------


async def _prepare(ref: str, *, admins: list[str] | None = None) -> knowledge.PackRead:
    ctx = SimpleNamespace(tenant_id="t-1", principal_id="p-1", iam_principal_id=None)
    settings = Settings(knowledge_pack_admins=admins or [])
    with patch.object(knowledge, "authorize", AsyncMock()) as authorize:
        read = await knowledge.prepare_pack_read(ctx, settings, ref)  # type: ignore[arg-type]
    if admins and not ref.startswith("tenant:"):
        authorize.assert_not_awaited()
    else:
        authorize.assert_awaited_once_with(
            ctx, Permission.EVENTS_READ, Permission.KNOWLEDGE_PACKS_MANAGE
        )
    return read


@pytest.mark.anyio
async def test_a_shared_pack_is_read_by_its_pinned_reference() -> None:
    read = await _prepare("software-delivery@1")
    answer = CONTRACT["packages"]["software-delivery@1"]
    request, result = await _pack(read, answer=answer)
    assert request.method == "GET"
    assert request.url.path == "/api/memory/packages/software-delivery"
    assert dict(request.url.params) == {"version": "1"}
    assert set(dict(request.url.params)) <= set(PACKAGE_PARAMS)
    assert result == answer
    KnowledgePackOut.model_validate(result)


@pytest.mark.anyio
async def test_a_name_alone_reads_the_latest_version() -> None:
    read = await _prepare("software-delivery")
    request, _ = await _pack(read, answer=CONTRACT["packages"]["software-delivery@1"])
    assert dict(request.url.params) == {}


@pytest.mark.anyio
async def test_a_tenant_pack_is_read_in_the_tenants_namespace() -> None:
    read = await _prepare("tenant:fleet@2")
    assert read == knowledge.PackRead(
        ref="tenant:fleet@2", name="tenant:fleet", version="2", namespace="tenant:t-1"
    )
    # Memory's pack_payload of a tenant pack: the spec plus scope, owner and ref.
    answer = {
        "name": "fleet",
        "version": "2",
        "kinds": [{"kind": "vehicle"}],
        "relations": [],
        "scope": "tenant",
        "namespace": "tenant:t-1",
        "ref": "tenant:fleet@2",
    }
    request, result = await _pack(read, answer=answer)
    assert request.url.raw_path.decode().startswith("/api/memory/packages/tenant%3Afleet?")
    assert dict(request.url.params) == {"version": "2", "namespace": "tenant:t-1"}
    assert set(dict(request.url.params)) <= set(PACKAGE_PARAMS)
    # The owner namespace stays with the core.
    assert "namespace" not in result
    assert result["scope"] == "tenant" and result["ref"] == "tenant:fleet@2"
    KnowledgePackOut.model_validate(result)


@pytest.mark.anyio
async def test_a_platform_pack_admin_reads_a_shared_pack_without_tenant_rights() -> None:
    read = await _prepare("software-delivery@1", admins=["p-1"])
    assert read.namespace == ""
    # A tenant pack still takes the tenant's right.
    await _prepare("tenant:fleet@2", admins=["p-1"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "ref", ["", "Selfdev@1", "selfdev@", "@1", "tenant:", "other:selfdev@1", "a/b@1", "x" * 65]
)
async def test_a_malformed_reference_is_refused_before_memory(ref: str) -> None:
    ctx = SimpleNamespace(tenant_id="t-1", principal_id="p-1", iam_principal_id=None)
    with (
        patch.object(knowledge, "authorize", AsyncMock()) as authorize,
        pytest.raises(ValidationError) as info,
    ):
        await knowledge.prepare_pack_read(ctx, Settings(), ref)  # type: ignore[arg-type]
    assert info.value.code == "invalid_pack_ref"
    authorize.assert_not_awaited()


@pytest.mark.anyio
async def test_an_unknown_pack_is_not_found() -> None:
    read = await _prepare("nope@1")
    with pytest.raises(NotFoundError) as info:
        await _pack(read, status=404, answer={"detail": "Пакет nope@1 не найден"})
    assert info.value.details == {"pack": "nope@1"}


@pytest.mark.anyio
async def test_a_memory_failure_on_a_pack_is_memorys() -> None:
    read = await _prepare("selfdev@1")
    with pytest.raises(UpstreamError) as info:
        await _pack(read, status=500, answer={"detail": "boom"})
    assert info.value.code == "memory_unavailable"
