"""The company-knowledge contract of the core (CP-ADR-0060, 0072, 0076; K003).

What is pinned here: the snapshot preview, ``expectedState``, knowledge base
documents and tenant packs in OpenAPI (routes, bodies, a 501 until their step:
the preview and ``expectedState`` are implemented by K008, documents by K009),
the whole document being valid OpenAPI 3.1 schema-wise, and the right
``knowledge.packs.manage`` in the enum and ``authz/catalog.yaml``.
"""

from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    KnowledgeDocumentRequest,
    KnowledgePackRegisterRequest,
    KnowledgeSnapshotPreviewRequest,
    KnowledgeSnapshotRequest,
)
from control_plane.domain.enums import Permission

ROOT = Path(__file__).resolve().parents[2]


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


OPENAPI = _openapi()
PATHS: dict[str, Any] = OPENAPI["paths"]
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]


def _ref(schema: dict[str, Any]) -> str:
    return str(schema["$ref"]).rsplit("/", 1)[-1]


def _body(path: str, method: str) -> str | None:
    body = PATHS[path][method].get("requestBody")
    return _ref(body["content"]["application/json"]["schema"]) if body else None


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [ref for value in node.values() for ref in _refs(value)]
    if isinstance(node, list):
        return [ref for value in node for ref in _refs(value)]
    return []


# --- OpenAPI -------------------------------------------------------------------


def test_openapi_is_valid() -> None:
    """OpenAPI 3.1: every schema is a valid JSON Schema 2020-12 and every
    ``$ref`` resolves to a published component."""
    assert OPENAPI["openapi"].startswith("3.1")
    for name, schema in SCHEMAS.items():
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:  # pragma: no cover - the message is the point
            pytest.fail(f"{name}: {exc.message}")
    prefix = "#/components/schemas/"
    for ref in _refs(OPENAPI):
        assert ref.startswith(prefix), ref
        assert ref.removeprefix(prefix) in SCHEMAS, ref


# (path, method, request body, step that implements it, implemented)
ROUTES = [
    (
        "/api/v1/knowledge/snapshots:preview",
        "post",
        "KnowledgeSnapshotPreviewRequest",
        "K008",
        True,
    ),
    ("/api/v1/knowledge/documents", "post", "KnowledgeDocumentRequest", "K009", True),
    (
        "/api/v1/knowledge/entities:query",
        "post",
        "KnowledgeEntitiesQueryRequest",
        "K031",
        True,
    ),
    # Existing routes that gained a field answered with 501 until its step.
    ("/api/v1/knowledge/snapshots", "post", "KnowledgeSnapshotRequest", "K008", True),
    ("/api/v1/knowledge/packs", "post", "KnowledgePackRegisterRequest", "K010", True),
]


@pytest.mark.parametrize(("path", "method", "request_body", "step", "implemented"), ROUTES)
def test_openapi_carries_the_route_with_its_body(
    path: str, method: str, request_body: str, step: str, implemented: bool
) -> None:
    assert _body(path, method) == request_body
    responses = set(PATHS[path][method]["responses"])
    assert {"403", "502", "503"} <= responses, step
    assert ("501" in responses) is not implemented, step


@pytest.mark.parametrize(
    "path", ["/api/v1/knowledge/snapshots", "/api/v1/knowledge/snapshots:preview"]
)
def test_the_snapshot_routes_publish_snapshot_stale(path: str) -> None:
    assert "snapshot_stale" in PATHS[path]["post"]["responses"]["409"]["description"]


def test_documents_are_implemented() -> None:
    """K009: the document route carries its body and no longer a 501."""
    path = "/api/v1/knowledge/documents"
    assert _body(path, "post") == "KnowledgeDocumentRequest"
    responses = PATHS[path]["post"]["responses"]
    assert {"403", "502", "503"} <= set(responses)
    assert "501" not in responses


def test_tenant_packs_are_implemented() -> None:
    """K010: pack registration carries its body and no longer a 501."""
    path = "/api/v1/knowledge/packs"
    assert _body(path, "post") == "KnowledgePackRegisterRequest"
    responses = PATHS[path]["post"]["responses"]
    assert {"403", "502", "503"} <= set(responses)
    assert "501" not in responses


def test_the_preview_answers_with_the_state_it_was_computed_on() -> None:
    ok = PATHS["/api/v1/knowledge/snapshots:preview"]["post"]["responses"]["200"]
    assert _ref(ok["content"]["application/json"]["schema"]) == "KnowledgeSnapshotPreviewOut"
    preview = SCHEMAS["KnowledgeSnapshotPreviewOut"]
    assert preview["required"] == ["stateToken"]
    # The rest of the plan is Memory's answer as-is.
    assert preview.get("additionalProperties", True) is True


def test_the_snapshot_takes_expected_state_and_the_preview_does_not() -> None:
    snapshot = SCHEMAS["KnowledgeSnapshotRequest"]
    assert "expectedState" in snapshot["properties"]
    assert "expectedState" not in snapshot.get("required", [])
    assert "expectedState" not in SCHEMAS["KnowledgeSnapshotPreviewRequest"]["properties"]
    # Same snapshot fields otherwise.
    preview = set(SCHEMAS["KnowledgeSnapshotPreviewRequest"]["properties"])
    assert set(snapshot["properties"]) == preview | {"expectedState"}


def test_expected_state_never_reaches_the_snapshot_document() -> None:
    body = {
        "workspaceId": "00000000-0000-0000-0000-000000000001",
        "source": "table:offering",
        "snapshotId": "s1",
        "observedAt": "2026-09-28T10:00:00Z",
        "expectedState": "sha256:abc",
    }
    document = KnowledgeSnapshotRequest.model_validate(body).snapshot_document()
    assert "expectedState" not in document
    assert "workspaceId" not in document
    with pytest.raises(ValidationError):
        KnowledgeSnapshotPreviewRequest.model_validate(body)


def test_a_document_is_chunks_and_typed_links() -> None:
    document = SCHEMAS["KnowledgeDocumentRequest"]
    assert set(document["properties"]) == {
        "workspaceId",
        "naturalKey",
        "title",
        "type",
        "chunks",
        "links",
        "meta",
    }
    assert set(document["required"]) == {"workspaceId", "naturalKey", "title", "chunks"}
    assert document["properties"]["chunks"]["maxItems"] == 500
    assert set(SCHEMAS["KnowledgeDocumentLink"]["required"]) == {"kind", "key", "rel"}
    assert set(SCHEMAS["KnowledgeDocumentChunk"]["properties"]) == {"text", "heading", "order"}
    ok = {
        "workspaceId": "00000000-0000-0000-0000-000000000001",
        "naturalKey": "document:license-1",
        "title": "License",
        "chunks": [{"text": "..."}],
        "links": [{"kind": "credential", "key": "license-1", "rel": "evidenced_by"}],
    }
    KnowledgeDocumentRequest.model_validate(ok)
    # The core parses no files: a document without text is refused.
    with pytest.raises(ValidationError):
        KnowledgeDocumentRequest.model_validate({**ok, "chunks": []})
    with pytest.raises(ValidationError):
        KnowledgeDocumentRequest.model_validate({**ok, "namespace": "tenant:x"})


def test_a_pack_manifest_names_its_scope_and_passes_the_rest() -> None:
    scope = SCHEMAS["KnowledgePackRegisterRequest"]["properties"]["scope"]
    assert scope["anyOf"] == [
        {"type": "string", "enum": ["common", "tenant"]},
        {"type": "null"},
    ]
    manifest = {"name": "fleet", "version": "1", "scope": "tenant", "kinds": [{"kind": "x"}]}
    parsed = KnowledgePackRegisterRequest.model_validate(manifest)
    assert parsed.model_dump(exclude_unset=True) == manifest
    common = {**manifest, "scope": "common"}
    assert (
        KnowledgePackRegisterRequest.model_validate(common).model_dump(exclude_unset=True) == common
    )
    with pytest.raises(ValidationError):
        KnowledgePackRegisterRequest.model_validate({**manifest, "scope": "platform"})


# --- permissions -----------------------------------------------------------------


def test_knowledge_packs_manage_is_in_the_enum_and_the_catalog() -> None:
    assert Permission.KNOWLEDGE_PACKS_MANAGE.value == "knowledge.packs.manage"
    catalog = yaml.safe_load((ROOT / "authz" / "catalog.yaml").read_text("utf-8"))
    assert catalog["actions"]["knowledge.packs.manage"] == {"resource": "tenant"}
