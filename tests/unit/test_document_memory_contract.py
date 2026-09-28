"""Case documents against Memory's pinned document-ingest contract (CP-ADR-0076 §2).

``tests/fixtures/memory_document_contract.json`` holds memory-service's
``DocumentIngestRequest``. The body is built by the adapter's own code
(artifact -> document request -> provider -> wire), captured by a mock
transport and validated against it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

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
