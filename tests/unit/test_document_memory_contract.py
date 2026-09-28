"""Documents against Memory's pinned document-ingest contract.

``tests/fixtures/memory_document_contract.json`` holds memory-service's
``DocumentIngestRequest``. Two writers use it: the Context Adapter for case
documents (CP-ADR-0076 §2; artifact -> document request -> provider -> wire)
and ``POST /knowledge/documents`` for knowledge base documents (CP-ADR-0060
amendment 2026-09-28, K3; request schema -> provider -> wire). Each body is
captured by a mock transport and validated against the pin.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

from control_plane.api.v1.schemas import KnowledgeDocumentRequest
from control_plane.infrastructure.context_provider.http import HttpContextProvider
from control_plane.infrastructure.db.models import Artifact
from control_plane.worker.context_adapter import ContextAdapter

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_document_contract.json").read_text()
)
PATH = "/api/brain/documents"


@pytest.mark.anyio
async def test_a_case_document_is_memorys_document_ingest_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"natural_key": "document:x", "chunks": 1})

    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    adapter = object.__new__(ContextAdapter)
    adapter.provider = provider
    adapter.content_store = None
    artifact = Artifact(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        type="sample-document",
        name="Notice",
        content={"text": "The notice."},
        content_state="none",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    key = f"document:artifact/{artifact.id}"
    try:
        await adapter._ingest_document(
            artifact.tenant_id,
            artifact,
            key,
            "tenant:t",
            {"instanceId": str(uuid.uuid4()), "definitionKey": "sample-case"},
        )
    finally:
        await provider.aclose()

    [request] = seen
    assert request.method == "POST" and request.url.path == PATH
    body = json.loads(request.content)
    schema = CONTRACT["schemas"][CONTRACT["paths"][PATH]["post"]["requestBody"]]
    Draft202012Validator(schema).validate(body)
    assert body["natural_key"] == key
    assert body["scope"] == {"namespace": "tenant:t"}
    assert body["replace"] is True
    assert "The notice." in body["chunks"][0]["text"]


@pytest.mark.anyio
async def test_a_knowledge_document_is_memorys_document_ingest_request() -> None:
    """K009 (CP-ADR-0060 amendment, K3): the request schema -> provider -> wire."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"natural_key": "document:license-1", "chunks": 2})

    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    links = [
        {"kind": "credential", "key": "credential:license-1", "rel": "evidenced_by"},
        {"kind": "organization", "key": "org:acme", "rel": "issued_to"},
        {"kind": "organization", "key": "org:acme", "rel": "mentions"},
    ]
    payload = KnowledgeDocumentRequest.model_validate(
        {
            "workspaceId": str(uuid.uuid4()),
            "naturalKey": "document:license-1",
            "title": "License",
            "type": "license",
            "chunks": [{"text": "License No. 1", "heading": "Terms", "order": 0}, {"text": "."}],
            "links": links,
            "meta": {"collection": "licenses"},
        }
    )
    try:
        answer = await provider.store_document(
            namespace="tenant:t:ws:r",
            scopes=["workspace:w"],
            document=payload.memory_document(),
        )
    finally:
        await provider.aclose()

    assert answer["chunks"] == 2
    [request] = seen
    assert request.method == "POST" and request.url.path == PATH
    body = json.loads(request.content)
    schema = CONTRACT["schemas"][CONTRACT["paths"][PATH]["post"]["requestBody"]]
    Draft202012Validator(schema).validate(body)
    # Namespace at the top level (Memory's ``_document_namespace``), no scope object.
    assert body["namespace"] == "tenant:t:ws:r"
    assert "scope" not in body
    # Visibility where Memory reads a node's: ``properties.scopes``.
    assert body["properties"] == {"links": links, "scopes": ["workspace:w"]}
    assert body["links"] == ["credential:license-1", "org:acme"]
    assert body["natural_key"] == "document:license-1"
    assert body["type"] == "license"
    assert body["meta"] == {"collection": "licenses"}
    assert body["replace"] is True
    assert body["chunks"][1] == {"text": ".", "heading": ""}
