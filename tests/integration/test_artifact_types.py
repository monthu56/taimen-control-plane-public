"""Artifact type registry (CP-ADR-0072 §6, artifact-handoff A004).

A type version is allocated by the server and never changes; an artifact of a
registered type is checked against the latest version of its key and records
that version, while an unregistered type (``commit``, ``transcript``...) is
accepted exactly as before the registry existed.
"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from control_plane.infrastructure.content_store import InMemoryContentStore
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap

INVOICE_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["number", "amount"],
    "properties": {
        "number": {"type": "string", "minLength": 1},
        "amount": {"type": "number", "minimum": 0},
    },
}


async def create_type(
    client: httpx.AsyncClient, key: str, *, type_key: str = "invoice", **extra: Any
) -> httpx.Response:
    body: dict[str, Any] = {
        "key": type_key,
        "displayName": type_key.title(),
        "metadataSchema": INVOICE_METADATA_SCHEMA,
        "mediaTypes": ["application/pdf", "image/*"],
        "maxBytes": 1_000_000,
    }
    body.update(extra)
    return await client.post("/api/v1/artifact-types", json=body, headers=auth(key))


async def create_artifact(
    client: httpx.AsyncClient, key: str, task_id: str, *, type_: str, **extra: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/artifacts",
        json={"task": task_id, "type": type_, "name": f"{type_}-1", **extra},
        headers=auth(key),
    )


async def test_versions_are_allocated_by_the_server_and_resolved_by_key(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    first = await create_type(client, admin_key)
    second = await create_type(client, admin_key, mediaTypes=["application/pdf"])

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert (first.json()["version"], second.json()["version"]) == (1, 2)
    assert first.json()["mediaTypes"] == ["application/pdf", "image/*"]
    assert first.json()["status"] == "active"

    latest = await client.get("/api/v1/artifact-types/invoice", headers=auth(admin_key))
    pinned = await client.get("/api/v1/artifact-types/invoice@1", headers=auth(admin_key))
    assert latest.json()["version"] == 2
    assert pinned.json()["version"] == 1
    assert pinned.json()["metadataSchema"] == INVOICE_METADATA_SCHEMA
    for missing in ("invoice@3", "invoice@x", "invoice@", "nothing"):
        response = await client.get(f"/api/v1/artifact-types/{missing}", headers=auth(admin_key))
        assert response.status_code == 404, missing

    listed = await client.get(
        "/api/v1/artifact-types", params={"key": "invoice"}, headers=auth(admin_key)
    )
    page = listed.json()
    assert [t["version"] for t in page["items"]] == [2, 1]
    assert (
        await client.get(
            "/api/v1/artifact-types", params={"status": "deprecated"}, headers=auth(admin_key)
        )
    ).json()["items"] == []


async def test_max_bytes_defaults_to_the_global_ceiling(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    body = {"key": "note", "displayName": "Note", "mediaTypes": ["text/markdown"]}
    created = await client.post("/api/v1/artifact-types", json=body, headers=auth(admin_key))

    assert created.status_code == 201, created.text
    assert created.json()["maxBytes"] == 104_857_600
    assert created.json()["metadataSchema"] == {}


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"mediaTypes": []}, "mediaTypes"),
        ({"mediaTypes": ["pdf"]}, "mediaTypes[0]"),
        ({"mediaTypes": ["application/pdf", "*/pdf"]}, "mediaTypes[1]"),
        ({"maxBytes": 104_857_601}, "maxBytes"),
        ({"maxBytes": 0}, "maxBytes"),
        ({"metadataSchema": {"type": "no-such-type"}}, "metadataSchema"),
        ({"metadataSchema": {"$ref": "https://example.com/schema"}}, "metadataSchema"),
        (
            {"metadataSchema": {"description": "x" * (16 * 1024)}},
            "metadataSchema",
        ),
    ],
)
async def test_invalid_definition_is_invalid_artifact_type(
    client: httpx.AsyncClient, extra: dict[str, Any], field: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await create_type(client, admin_key, **extra)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_artifact_type"
    assert error["details"]["field"] == field
    listed = (await client.get("/api/v1/artifact-types", headers=auth(admin_key))).json()
    assert listed["items"] == []


async def test_registry_has_permissions_of_its_own(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_type(client, admin_key)
    _, writer_key = await create_agent_with_key(
        client, admin_key, permissions=["tasks.read", "artifacts.read", "artifacts.write"]
    )
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["artifact_types.read"]
    )

    assert (await create_type(client, writer_key, type_key="other")).status_code == 403
    assert (await client.get("/api/v1/artifact-types", headers=auth(writer_key))).status_code == 403
    assert (
        await client.get("/api/v1/artifact-types/invoice", headers=auth(writer_key))
    ).status_code == 403
    assert (await client.get("/api/v1/artifact-types", headers=auth(reader_key))).status_code == 200
    assert (await create_type(client, reader_key, type_key="other")).status_code == 403


async def test_artifact_type_does_not_cross_the_tenant_boundary(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    from tests.helpers import make_tenant_directly

    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_type(client, admin_key)
    _, other_key = make_tenant_directly(sync_engine, "other")

    assert (
        await client.get("/api/v1/artifact-types/invoice", headers=auth(other_key))
    ).status_code == 404
    assert (await client.get("/api/v1/artifact-types", headers=auth(other_key))).json()[
        "items"
    ] == []
    # The other tenant's registry does not check this tenant's artifacts.
    task = await create_task(client, other_key)
    response = await create_artifact(
        client, other_key, task["id"], type_="invoice", metadata={"anything": True}
    )
    assert response.status_code == 201, response.text
    assert response.json()["typeVersion"] is None


async def test_version_content_is_immutable_even_against_raw_sql(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await create_type(client, admin_key)).json()

    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE artifact_types SET max_bytes = 5 WHERE id = :id"), {"id": created["id"]}
        )
    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE artifact_types SET media_types = '[\"*/*\"]'::jsonb WHERE id = :id"),
            {"id": created["id"]},
        )
    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM artifact_types WHERE id = :id"), {"id": created["id"]})
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE artifact_types SET status = 'deprecated' WHERE id = :id"),
            {"id": created["id"]},
        )
    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE artifact_types SET status = 'active' WHERE id = :id"),
            {"id": created["id"]},
        )


async def test_creation_is_recorded_as_an_event(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await create_type(client, admin_key)).json()

    events = (
        await client.get(
            "/api/v1/events", params={"types": "artifact_type.created"}, headers=auth(admin_key)
        )
    ).json()["items"]

    assert len(events) == 1
    assert events[0]["entityType"] == "artifact_type"
    assert events[0]["entityId"] == created["id"]
    assert events[0]["payload"] == {
        "key": "invoice",
        "version": 1,
        "mediaTypes": ["application/pdf", "image/*"],
        "maxBytes": 1_000_000,
        "declaresMetadataSchema": True,
    }


async def test_artifact_of_a_registered_type_is_checked_and_records_the_version(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_type(client, admin_key)
    await create_type(client, admin_key)
    task = await create_task(client, admin_key)

    good = await create_artifact(
        client,
        admin_key,
        task["id"],
        type_="invoice",
        uri="https://erp.example.com/invoices/42",
        metadata={"number": "42", "amount": 100.5},
    )
    assert good.status_code == 201, good.text
    assert good.json()["typeVersion"] == 2

    bad = await create_artifact(
        client, admin_key, task["id"], type_="invoice", metadata={"number": "", "amount": -1}
    )
    assert bad.status_code == 422, bad.text
    error = bad.json()["error"]
    assert error["code"] == "invalid_artifact_metadata"
    assert {e["path"] for e in error["details"]["errors"]} == {"/number", "/amount"}

    missing = await create_artifact(client, admin_key, task["id"], type_="invoice")
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_artifact_metadata"

    listed = (
        await client.get(
            "/api/v1/artifacts", params={"taskId": task["id"]}, headers=auth(admin_key)
        )
    ).json()["items"]
    assert [a["id"] for a in listed] == [good.json()["id"]]


async def upload(client: httpx.AsyncClient, key: str, data: bytes, media_type: str) -> str:
    response = await client.put(
        "/api/v1/artifact-contents",
        content=data,
        headers={**auth(key), "Content-Type": media_type},
    )
    assert response.status_code == 201, response.text
    return response.json()["contentRef"]


async def test_stored_content_is_checked_on_media_type_and_size(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    app.state.content_store = InMemoryContentStore()
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_type(client, admin_key, maxBytes=16)
    task = await create_task(client, admin_key)
    metadata = {"number": "42", "amount": 1}

    wrong_type = await upload(client, admin_key, b"plain", "text/plain")
    rejected = await create_artifact(
        client, admin_key, task["id"], type_="invoice", contentRef=wrong_type, metadata=metadata
    )
    assert rejected.status_code == 422, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "media_type_not_allowed"
    assert error["details"]["mediaType"] == "text/plain"

    too_large = await upload(client, admin_key, b"%PDF" + b"x" * 16, "application/pdf")
    rejected = await create_artifact(
        client, admin_key, task["id"], type_="invoice", contentRef=too_large, metadata=metadata
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "artifact_too_large"
    assert rejected.json()["error"]["details"]["maxBytes"] == 16

    # A rejected artifact leaves the upload unclaimed: it can still be used.
    reused = await create_artifact(
        client, admin_key, task["id"], type_="report", contentRef=wrong_type
    )
    assert reused.status_code == 201, reused.text
    assert reused.json()["typeVersion"] is None

    fits = await upload(client, admin_key, b"image", "image/png")
    accepted = await create_artifact(
        client, admin_key, task["id"], type_="invoice", contentRef=fits, metadata=metadata
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["typeVersion"] == 1
    assert accepted.json()["contentState"] == "stored"


async def test_the_latest_version_is_what_checks(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_type(client, admin_key)
    await create_type(client, admin_key, metadataSchema={})
    task = await create_task(client, admin_key)

    response = await create_artifact(
        client, admin_key, task["id"], type_="invoice", metadata={"free": "form"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["typeVersion"] == 2


async def test_unregistered_types_work_as_before(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_type(client, admin_key)
    task = await create_task(client, admin_key)

    for type_ in ("commit", "transcript", "report"):
        response = await create_artifact(
            client,
            admin_key,
            task["id"],
            type_=type_,
            uri="git://repo@abc",
            content={"lines": 3},
            metadata={"whatever": [1, 2]},
        )
        assert response.status_code == 201, response.text
        assert response.json()["typeVersion"] is None
