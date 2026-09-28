"""The core's knowledge requests against Memory's pinned request contract (CP-ADR-0060).

``tests/fixtures/memory_knowledge_contract.json`` holds the request models of
the Memory Service knowledge endpoints as its ``app.openapi()`` publishes them
(``ReconcileIn``, ``PackIn``, ``NamespaceKindsIn``) plus the snapshot fields
``domain.reconcile.parse_snapshot`` reads from the TOP level of ``ReconcileIn``.
The bodies are built by the same code the endpoints run (request schema ->
provider -> wire), captured by a mock transport and validated against it. The
live check against a running Memory Service is ``tests/contract``.
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
from jsonschema import Draft202012Validator

from control_plane.api.v1.schemas import KNOWLEDGE_MAX_ITEMS, KnowledgeSnapshotRequest
from control_plane.application.commands import knowledge
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ConflictError, UpstreamError, ValidationError
from control_plane.infrastructure.context_provider.http import HttpContextProvider
from tests.helpers import knowledge_snapshot

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_knowledge_contract.json").read_text()
)


def _validate(schema: dict[str, Any], body: object) -> None:
    Draft202012Validator(schema).validate(body)


def _request_schema(path: str, method: str) -> dict[str, Any]:
    name = CONTRACT["paths"][path][method]["requestBody"]
    return CONTRACT["schemas"][name]


async def _captured(call: str, **kwargs: Any) -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        await getattr(provider, call)(**kwargs)
    finally:
        await provider.aclose()
    [request] = seen
    return request


@pytest.mark.anyio
async def test_reconcile_body_is_memorys_reconcile_in() -> None:
    payload = KnowledgeSnapshotRequest.model_validate(
        {**knowledge_snapshot(), "workspaceId": "00000000-0000-0000-0000-000000000001"}
    )
    request = await _captured(
        "reconcile_snapshot",
        namespace="tenant:t:ws:r",
        scopes=["workspace:w"],
        snapshot=payload.snapshot_document(),
    )
    path = "/api/memory/reconcile"
    assert request.method == "POST" and request.url.path == path
    assert "post" in CONTRACT["paths"][path]
    body = json.loads(request.content)
    _validate(_request_schema(path, "post"), body)
    assert body["namespace"] == "tenant:t:ws:r"
    assert body["scopes"] == ["workspace:w"]
    # Memory reads the snapshot as ``model_dump(exclude={"namespace", "scopes"})``.
    document = {k: v for k, v in body.items() if k not in ("namespace", "scopes")}
    assert "snapshot" not in document
    _validate(CONTRACT["snapshotDocument"], document)
    assert set(document) <= set(CONTRACT["snapshotDocument"]["properties"])


def test_snapshot_bounds_match_memory() -> None:
    fields = CONTRACT["snapshotDocument"]["properties"]
    schema = KnowledgeSnapshotRequest.model_json_schema(by_alias=True)["properties"]
    for name in ("source", "snapshotId"):
        assert schema[name]["maxLength"] == fields[name]["maxLength"], name
    scope_types = {branch.get("type") for branch in schema["scope"]["anyOf"]}
    assert scope_types == {"string", "null"}
    assert [b["maxLength"] for b in schema["scope"]["anyOf"] if b.get("type") == "string"] == [
        fields["scope"]["maxLength"]
    ]
    assert CONTRACT["snapshotDocument"]["maxItemsTotal"] == KNOWLEDGE_MAX_ITEMS


@pytest.mark.anyio
async def test_package_body_is_memorys_pack_in() -> None:
    manifest = {"name": "selfdev", "version": 1, "kinds": [{"kind": "module"}]}
    request = await _captured("register_package", package=manifest)
    path = "/api/memory/packages"
    assert request.method == "POST" and request.url.path == path
    _validate(_request_schema(path, "post"), json.loads(request.content))


@pytest.mark.anyio
async def test_tenant_package_body_is_memorys_pack_in() -> None:
    """K010: the tenant pack as the core forwards it -- the manifest plus
    ``scope: tenant`` and the owner namespace the core computed."""
    manifest = {
        "name": "fleet",
        "version": 1,
        "scope": "tenant",
        "kinds": [{"kind": "vehicle", "naturalKey": "<plate>"}],
        "relations": [{"relation": "serves", "fromKinds": ["vehicle"], "toKinds": ["team"]}],
    }
    ctx = SimpleNamespace(tenant_id="t-1")
    with patch.object(knowledge, "authorize", AsyncMock()) as authorize:
        package = await knowledge.prepare_pack(ctx, Settings(), manifest)  # type: ignore[arg-type]
    authorize.assert_awaited_once_with(ctx, Permission.KNOWLEDGE_PACKS_MANAGE)
    request = await _captured("register_package", package=package)
    schema = _request_schema("/api/memory/packages", "post")
    body = json.loads(request.content)
    _validate(schema, body)
    assert body["scope"] == "tenant"
    assert body["namespace"] == "tenant:t-1"
    assert {"scope", "namespace"} <= set(schema["properties"])


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [None, "common"])
async def test_a_shared_pack_takes_the_platform_admin_path(scope: str | None) -> None:
    """``scope: common`` is Memory's name of a shared pack: the same path as no scope."""
    manifest: dict[str, object] = {"name": "company", "version": 1}
    if scope is not None:
        manifest["scope"] = scope
    ctx = SimpleNamespace(tenant_id="t-1", principal_id="admin", iam_principal_id=None)
    settings = Settings(knowledge_pack_admins=["admin"])
    with patch.object(knowledge, "authorize", AsyncMock()) as authorize:
        package = await knowledge.prepare_pack(ctx, settings, manifest)  # type: ignore[arg-type]
    authorize.assert_not_awaited()
    assert "namespace" not in package
    request = await _captured("register_package", package=package)
    _validate(_request_schema("/api/memory/packages", "post"), json.loads(request.content))


PACK_CONFLICT = CONTRACT["responses"]["packages"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "detail",
    PACK_CONFLICT["PackScopeConflict"]["$defs"]["PackScopeConflictDetail"]["properties"]["code"][
        "enum"
    ],
)
async def test_a_tenant_pack_name_clash_is_pack_invalid(detail: str) -> None:
    answer = {"detail": {"code": detail, "message": "taken by a shared pack"}}
    _validate(PACK_CONFLICT["PackScopeConflict"], answer)
    assert detail in knowledge.PACK_SCOPE_CONFLICTS
    provider = _answering(PACK_CONFLICT["conflictStatus"], answer)
    try:
        with pytest.raises(ValidationError) as info:
            await knowledge.register_pack(provider, {"name": "fleet", "version": "1"})
    finally:
        await provider.aclose()
    assert info.value.code == "pack_invalid"
    assert info.value.details == {"memoryStatus": 409, "conflict": detail}


@pytest.mark.anyio
async def test_a_version_conflict_stays_a_conflict() -> None:
    # Memory's immutability 409 keeps its string detail.
    answer = {"detail": "version 1 exists with other content"}
    _validate(PACK_CONFLICT["PackScopeConflict"], answer)
    provider = _answering(409, answer)
    try:
        with pytest.raises(ConflictError) as info:
            await knowledge.register_pack(provider, {"name": "fleet", "version": "1"})
    finally:
        await provider.aclose()
    assert info.value.code == "pack_version_conflict"


def _answering(status: int, body: dict[str, Any]) -> HttpContextProvider:
    return HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    )


@pytest.mark.anyio
async def test_namespace_kinds_body_is_memorys_namespace_kinds_in() -> None:
    request = await _captured(
        "set_namespace_kinds", namespace="tenant:t:ws:r", packages=["selfdev@1"], strict=True
    )
    assert request.method == "PUT"
    assert request.url.path == "/api/memory/namespaces/tenant:t:ws:r/kinds"
    schema = _request_schema("/api/memory/namespaces/{namespace}/kinds", "put")
    body = json.loads(request.content)
    _validate(schema, body)
    # NamespaceKindsIn ignores unknown fields: a typo would silently drop the set.
    assert set(body) <= set(schema["properties"])


def test_pack_is_optional_on_both_sides() -> None:
    # Memory's snapshot ``pack`` defaults to "" (kinds from the namespace catalog).
    assert "pack" not in CONTRACT["snapshotDocument"]["required"]
    schema = KnowledgeSnapshotRequest.model_json_schema(by_alias=True)
    assert "pack" not in schema.get("required", [])
    payload = KnowledgeSnapshotRequest.model_validate(
        {
            **{k: v for k, v in knowledge_snapshot().items() if k != "pack"},
            "workspaceId": "00000000-0000-0000-0000-000000000001",
        }
    )
    document = payload.snapshot_document()
    assert "pack" not in document
    _validate(CONTRACT["snapshotDocument"], document)


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"name": "selfdev"},
        {"version": 1},
        {"name": "", "version": 1},
        {"name": "selfdev", "version": True},
        {"name": "selfdev", "version": None},
        {"name": 5, "version": "1"},
    ],
)
def test_manifest_without_identity_is_refused_before_memory(manifest: dict[str, Any]) -> None:
    # Whatever PackIn refuses with its own 422 the core refuses first as
    # pack_invalid, so the client never sees Memory's model error as a 502.
    with pytest.raises(ValidationError) as info:
        knowledge.require_pack_identity(manifest)
    assert info.value.code == "pack_invalid"


@pytest.mark.parametrize("version", ["1", 1, 1.5])
def test_manifest_with_identity_passes_pack_in(version: object) -> None:
    manifest = {"name": "selfdev", "version": version, "kinds": []}
    knowledge.require_pack_identity(manifest)
    _validate(_request_schema("/api/memory/packages", "post"), manifest)


# --- the answer of reconcile the core reads (CP-ADR-0076 §7, MEM-ADR-020) ------------

RECONCILE_ANSWER = CONTRACT["responses"]["reconcile"]


def test_changed_keys_read_memorys_reconcile_changes() -> None:
    example = RECONCILE_ANSWER["example"]
    changes = example[RECONCILE_ANSWER["changesField"]]
    _validate(RECONCILE_ANSWER["ReconcileChanges"], changes)
    assert list(knowledge.CHANGE_LISTS) == RECONCILE_ANSWER["changeLists"]
    assert set(knowledge.CHANGE_LISTS) <= set(RECONCILE_ANSWER["ReconcileChanges"]["properties"])
    assert knowledge.changed_keys(example) == (
        [{"kind": "ui_call", "key": "web-app:src/runs/page.tsx:38", "change": "opened"}],
        False,
    )


def test_changed_keys_follow_the_lists_and_their_cut() -> None:
    answer = {
        "duplicate": False,
        "changes": {
            "opened": [{"kind": "regulation", "key": "regulation:new"}],
            "changed": [{"kind": "regulation", "key": "regulation:procurement"}],
            "closed": [{"kind": "regulation", "key": "regulation:old"}, "not an entry"],
            "limit": 1000,
            "truncated": True,
        },
    }
    _validate(RECONCILE_ANSWER["ReconcileChanges"], {**answer["changes"], "closed": []})
    changes, truncated = knowledge.changed_keys(answer)
    assert [(c["key"], c["change"]) for c in changes] == [
        ("regulation:new", "opened"),
        ("regulation:procurement", "changed"),
        ("regulation:old", "closed"),
    ]
    assert truncated is True


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"changes": None},
        {"changes": {"opened": [], "changed": [], "closed": [], "limit": 1000}},
        # A repeated snapshot touched nothing, whatever the lists say.
        {"duplicate": True, "changes": {"opened": [{"kind": "doc", "key": "d"}]}},
    ],
)
def test_nothing_changed_is_no_keys(answer: dict[str, Any]) -> None:
    assert knowledge.changed_keys(answer) == ([], False)


def test_one_event_carries_a_bounded_list() -> None:
    many = [{"kind": "doc", "key": f"d{i}"} for i in range(knowledge.MAX_EVENT_CHANGES + 5)]
    changes, truncated = knowledge.changed_keys({"changes": {"opened": many}})
    assert len(changes) == knowledge.MAX_EVENT_CHANGES and truncated is True


# --- preview and apply by state (MEM-ADR-020 amendment 2026-09-28, K008) -----------

RECONCILE_STATE = CONTRACT["responses"]["reconcileState"]
# Memory reads the snapshot as ``model_dump(exclude={namespace, scopes, dry_run,
# expected_state})``: none of them is a field of the snapshot document.
_NOT_SNAPSHOT = ("namespace", "scopes", "dryRun", "expectedState")


async def _reconcile_body(**kwargs: Any) -> dict[str, Any]:
    payload = KnowledgeSnapshotRequest.model_validate(
        {**knowledge_snapshot(), "workspaceId": "00000000-0000-0000-0000-000000000001"}
    )
    request = await _captured(
        "reconcile_snapshot",
        namespace="tenant:t:ws:r",
        scopes=["workspace:w"],
        snapshot=payload.snapshot_document(),
        **kwargs,
    )
    body = json.loads(request.content)
    _validate(_request_schema("/api/memory/reconcile", "post"), body)
    document = {k: v for k, v in body.items() if k not in _NOT_SNAPSHOT}
    _validate(CONTRACT["snapshotDocument"], document)
    assert set(document) <= set(CONTRACT["snapshotDocument"]["properties"])
    return body


@pytest.mark.anyio
async def test_preview_body_asks_memory_for_a_dry_run() -> None:
    body = await _reconcile_body(dry_run=True)
    assert "dryRun" in CONTRACT["schemas"]["ReconcileIn"]["properties"]
    assert body["dryRun"] is True
    assert "expectedState" not in body


@pytest.mark.anyio
async def test_apply_body_carries_expected_state_next_to_the_snapshot() -> None:
    token = RECONCILE_STATE["example"]["stateToken"]
    body = await _reconcile_body(expected_state=token)
    assert "expectedState" in CONTRACT["schemas"]["ReconcileIn"]["properties"]
    assert body["expectedState"] == token
    assert "dryRun" not in body


@pytest.mark.anyio
async def test_a_plain_reconcile_body_is_unchanged() -> None:
    body = await _reconcile_body()
    assert "dryRun" not in body and "expectedState" not in body


def test_expected_state_bound_is_memorys() -> None:
    [memory] = [
        branch
        for branch in CONTRACT["schemas"]["ReconcileIn"]["properties"]["expectedState"]["anyOf"]
        if branch.get("type") == "string"
    ]
    schema = KnowledgeSnapshotRequest.model_json_schema(by_alias=True)["properties"]
    [ours] = [b for b in schema["expectedState"]["anyOf"] if b.get("type") == "string"]
    assert ours["maxLength"] == memory["maxLength"]


class _Answering:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer

    async def reconcile_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["dry_run"] is True
        return self.answer


_TARGET = knowledge.KnowledgeTarget(
    workspace_id=uuid.UUID(int=1),
    root_workspace_id=uuid.UUID(int=1),
    namespace="tenant:t:ws:r",
    scopes=["workspace:w"],
)


@pytest.mark.anyio
async def test_a_preview_answer_is_memorys_plan_with_its_state() -> None:
    fields = RECONCILE_STATE["planFields"]
    assert {b.get("type") for b in fields["stateToken"]["anyOf"]} == {"string", "null"}
    assert fields["dryRun"]["type"] == "boolean"
    example = RECONCILE_STATE["example"]
    _validate(RECONCILE_ANSWER["ReconcileChanges"], example["changes"])
    answer = await knowledge.preview_snapshot(
        _Answering(example),  # type: ignore[arg-type]
        _TARGET,
        knowledge_snapshot(),
    )
    assert answer == example


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        # A Memory before the preview ignores dryRun and applies the snapshot.
        RECONCILE_ANSWER["example"],
        {**RECONCILE_STATE["example"], "dryRun": False},
        {**RECONCILE_STATE["example"], "stateToken": None},
    ],
)
async def test_an_answer_that_is_not_a_plan_is_memorys_fault(answer: dict[str, Any]) -> None:
    with pytest.raises(UpstreamError) as info:
        await knowledge.preview_snapshot(
            _Answering(answer),  # type: ignore[arg-type]
            _TARGET,
            knowledge_snapshot(),
        )
    assert info.value.code == "memory_unavailable"
    assert info.value.details == {"memoryStatus": 200, "retryable": False}


def test_a_moved_state_is_memorys_409() -> None:
    assert "409" in CONTRACT["paths"]["/api/memory/reconcile"]["post"]["responses"]
    schema = RECONCILE_STATE["SnapshotStaleError"]
    _validate(schema, RECONCILE_STATE["staleExample"])
    assert schema["properties"]["detail"]["properties"]["code"]["const"] == "snapshot_stale"
