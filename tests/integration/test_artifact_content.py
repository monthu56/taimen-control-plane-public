"""Artifact content: upload, reference, read, purge, sweep (CP-ADR-0072, A003).

The store is the in-memory adapter put on the app in place of S3; the S3
adapter itself is pinned by ``tests/contract/test_content_store_contract.py``.
"""

import hashlib
import os
import tempfile
import tracemalloc
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.commands import artifacts as artifact_commands
from control_plane.application.commands.artifacts import sweep_expired_uploads
from control_plane.application.queries import execution as execution_queries
from control_plane.config import Settings
from control_plane.domain.errors import AuthorizationError
from control_plane.infrastructure.content_store import InMemoryContentStore, object_key
from control_plane.main import create_app
from control_plane.worker.main import Worker
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
)

MIB = 1024 * 1024
MAX_BYTES = 104_857_600


@pytest.fixture
def store(app: FastAPI) -> InMemoryContentStore:
    content_store = InMemoryContentStore()
    app.state.content_store = content_store
    return content_store


@pytest.fixture
async def boot(client: httpx.AsyncClient) -> dict[str, Any]:
    body = await do_bootstrap(client)
    admin = body["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin, name="writer", permissions=ORG_AGENT_PERMISSIONS
    )
    other, other_key = await create_agent_with_key(
        client, admin, name="other", permissions=ORG_AGENT_PERMISSIONS
    )
    return {
        "admin": admin,
        "tenantId": body["tenant"]["id"],
        "agent": agent_key,
        "agentId": agent["id"],
        "other": other_key,
        "otherId": other["id"],
    }


async def upload(
    client: httpx.AsyncClient,
    key: str,
    data: bytes | AsyncIterator[bytes],
    media_type: str | None = "application/pdf",
) -> httpx.Response:
    headers = auth(key)
    if media_type is not None:
        headers["Content-Type"] = media_type
    return await client.put("/api/v1/artifact-contents", content=data, headers=headers)


async def uploaded(client: httpx.AsyncClient, key: str, data: bytes, **kw: Any) -> dict[str, Any]:
    response = await upload(client, key, data, **kw)
    assert response.status_code == 201, response.text
    return response.json()


async def create_artifact(
    client: httpx.AsyncClient, key: str, body: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        "/api/v1/artifacts",
        json={"type": "report", "name": "report.pdf", **body},
        headers=auth(key),
    )


async def stored_artifact(
    client: httpx.AsyncClient, key: str, task_id: str, data: bytes, **kw: Any
) -> dict[str, Any]:
    ref = (await uploaded(client, key, data, **kw))["contentRef"]
    response = await create_artifact(client, key, {"task": task_id, "contentRef": ref})
    assert response.status_code == 201, response.text
    return response.json()


async def events_of(client: httpx.AsyncClient, key: str, type_: str) -> list[dict[str, Any]]:
    items = (await client.get("/api/v1/events?limit=200", headers=auth(key))).json()["items"]
    return [e for e in items if e["type"] == type_]


async def chunked(total: int, chunk: int = MIB) -> AsyncIterator[bytes]:
    """``total`` bytes without a Content-Length, generated chunk by chunk."""
    block = b"\xab" * chunk
    sent = 0
    while sent < total:
        piece = block[: min(chunk, total - sent)]
        sent += len(piece)
        yield piece


def spool_files() -> set[str]:
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("cp-artifact-")}


# --- upload and reference ----------------------------------------------------


async def test_upload_reference_and_read_back(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    task = await create_task(client, boot["admin"])
    data = b"%PDF-1.7 report body"
    sha = hashlib.sha256(data).hexdigest()

    up = await uploaded(client, boot["agent"], data, media_type="Application/PDF")
    assert up["contentRef"].startswith("cref_")
    assert up["sizeBytes"] == len(data)
    assert up["sha256"] == sha
    assert up["mediaType"] == "application/pdf"
    assert "expiresAt" in up
    assert store.objects[object_key(uuid.UUID(boot["tenantId"]), sha)] == data

    response = await create_artifact(
        client,
        boot["agent"],
        {"task": task["id"], "contentRef": up["contentRef"], "metadata": {"pages": 1}},
    )
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["contentState"] == "stored"
    assert artifact["sizeBytes"] == len(data)
    assert artifact["mediaType"] == "application/pdf"
    assert artifact["sha256"] == sha
    assert artifact["typeVersion"] is None
    assert artifact["uri"] is None and artifact["content"] is None

    read = await client.get(
        f"/api/v1/artifacts/{artifact['id']}/content", headers=auth(boot["agent"])
    )
    assert read.status_code == 200
    assert read.content == data
    assert read.headers["content-type"] == "application/pdf"
    assert read.headers["content-length"] == str(len(data))
    assert read.headers["etag"] == f'"sha256:{sha}"'
    assert read.headers["x-content-type-options"] == "nosniff"
    assert read.headers["cache-control"] == "private, no-store"
    assert read.headers["content-disposition"] == "inline; filename*=UTF-8''report.pdf"

    created = (await events_of(client, boot["admin"], "artifact.created"))[0]
    assert created["schemaVersion"] == 2
    assert created["payload"]["contentState"] == "stored"
    assert created["payload"]["sha256"] == sha
    assert created["payload"]["sizeBytes"] == len(data)
    assert data.decode() not in str(created["payload"])
    reads = await events_of(client, boot["admin"], "artifact.content_read")
    assert len(reads) == 1
    assert reads[0]["actorId"] == boot["agentId"]
    assert reads[0]["payload"] == {
        "artifactId": artifact["id"],
        "taskId": task["id"],
        "forTaskId": None,
        "runId": None,
        "sha256": sha,
        "sizeBytes": len(data),
    }


async def test_record_only_artifacts_are_unchanged(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    response = await create_artifact(
        client, boot["agent"], {"uri": "https://example.test/doc", "content": {"k": 1}}
    )
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["contentState"] == "none"
    assert artifact["sizeBytes"] is None and artifact["sha256"] is None
    assert artifact["mediaType"] is None

    read = await client.get(
        f"/api/v1/artifacts/{artifact['id']}/content", headers=auth(boot["agent"])
    )
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "content_not_found"
    created = (await events_of(client, boot["admin"], "artifact.created"))[0]
    assert created["payload"]["contentState"] == "none"
    assert created["payload"]["sha256"] is None


async def test_content_ref_excludes_content_and_uri(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    ref = (await uploaded(client, boot["agent"], b"x"))["contentRef"]
    for extra in ({"uri": "https://example.test"}, {"content": {"a": 1}}):
        response = await create_artifact(client, boot["agent"], {"contentRef": ref, **extra})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_artifact_content"


async def test_content_ref_is_bound_to_the_uploader_and_its_ttl(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    store: InMemoryContentStore,
    sync_engine: Engine,
) -> None:
    ref = (await uploaded(client, boot["agent"], b"secret bytes"))["contentRef"]

    def rejected(response: httpx.Response) -> bool:
        return (
            response.status_code == 422
            and response.json()["error"]["code"] == "content_ref_not_found"
        )

    # Another principal of the same tenant, unknown and malformed references.
    assert rejected(await create_artifact(client, boot["other"], {"contentRef": ref}))
    assert rejected(
        await create_artifact(client, boot["agent"], {"contentRef": f"cref_{uuid.uuid4()}"})
    )
    assert rejected(await create_artifact(client, boot["agent"], {"contentRef": "sha256:abc"}))
    # Another tenant.
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    assert rejected(await create_artifact(client, key_b, {"contentRef": ref}))

    # Expired.
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE artifact_contents SET expires_at = now() - interval '1 second'"))
    assert rejected(await create_artifact(client, boot["agent"], {"contentRef": ref}))


async def test_upload_needs_media_type_and_write_permission(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    missing = await upload(client, boot["agent"], b"x", media_type=None)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_request"
    bad = await upload(client, boot["agent"], b"x", media_type="not a type")
    assert bad.status_code == 400

    _, reader = await create_agent_with_key(
        client, boot["admin"], name="reader", permissions=["artifacts.read"]
    )
    assert (await upload(client, reader, b"x")).status_code == 403
    assert store.objects == {}


# --- store off / down --------------------------------------------------------


async def test_without_store_content_is_503_and_records_work(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    response = await upload(client, boot["agent"], b"x")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "content_store_unavailable"

    artifact = (await create_artifact(client, boot["agent"], {"content": {"a": 1}})).json()
    assert artifact["contentState"] == "none"
    listed = await client.get("/api/v1/artifacts", headers=auth(boot["agent"]))
    assert listed.status_code == 200


async def test_store_outage_is_503_and_leaves_no_trace(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    store: InMemoryContentStore,
    sync_engine: Engine,
) -> None:
    task = await create_task(client, boot["admin"])
    artifact = await stored_artifact(client, boot["agent"], task["id"], b"bytes")
    # Past the upload's TTL a purge has to delete the object, so it needs the store.
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE artifact_contents SET expires_at = now() - interval '1 second'"))
    store.available = False
    before = spool_files()

    assert (await upload(client, boot["agent"], b"more")).status_code == 503
    read = await client.get(
        f"/api/v1/artifacts/{artifact['id']}/content", headers=auth(boot["agent"])
    )
    assert read.status_code == 503
    assert read.json()["error"]["code"] == "content_store_unavailable"
    purge = await client.post(
        f"/api/v1/artifacts/{artifact['id']}:purge-content",
        json={"reason": "gdpr"},
        headers=auth(boot["admin"]),
    )
    assert purge.status_code == 503

    assert spool_files() <= before
    assert await events_of(client, boot["admin"], "artifact.content_read") == []
    assert await events_of(client, boot["admin"], "artifact.content_purged") == []
    again = await client.get(f"/api/v1/artifacts/{artifact['id']}", headers=auth(boot["agent"]))
    assert again.json()["contentState"] == "stored"


# --- size limit and memory ---------------------------------------------------


class _DigestStore(InMemoryContentStore):
    """Keeps a checksum of what it was given, not the bytes: the memory the
    test measures is the API's alone."""

    def __init__(self) -> None:
        super().__init__()
        self.digests: dict[str, str] = {}

    async def put(self, key: str, path: Path, size: int) -> None:
        digest = hashlib.sha256()
        read = 0
        with path.open("rb") as handle:
            while block := handle.read(MIB):
                digest.update(block)
                read += len(block)
        assert read == size
        self.digests[key] = digest.hexdigest()
        self.objects[key] = b""


async def test_limit_is_100_mib_and_memory_stays_flat(
    app: FastAPI, client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    store = _DigestStore()
    app.state.content_store = store
    before = spool_files()

    tracemalloc.start()
    try:
        response = await upload(client, boot["agent"], chunked(MAX_BYTES))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert response.status_code == 201, response.text
    assert response.json()["sizeBytes"] == MAX_BYTES
    expected = hashlib.sha256()
    async for piece in chunked(MAX_BYTES):
        expected.update(piece)
    assert response.json()["sha256"] == expected.hexdigest()
    assert list(store.digests.values()) == [expected.hexdigest()]
    # The body is 100 MiB; the process holds a few chunks of it at a time.
    assert peak < 16 * MIB, f"peak {peak / MIB:.1f} MiB"

    # One byte more: streamed (no Content-Length) and declared up front.
    too_big = await upload(client, boot["agent"], chunked(MAX_BYTES + 1))
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "request_too_large"
    declared = await client.put(
        "/api/v1/artifact-contents",
        headers={
            **auth(boot["agent"]),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(MAX_BYTES + 1),
        },
        content=b"",
    )
    assert declared.status_code == 413
    assert len(store.digests) == 1
    assert spool_files() <= before


async def test_spool_limit_follows_setting(
    app: FastAPI, client: httpx.AsyncClient, boot: dict[str, Any], settings: Settings
) -> None:
    """The route counts on its own too: a smaller ceiling holds even if the
    middleware limit were missing."""
    app.state.content_store = InMemoryContentStore()
    app.state.settings = settings.model_copy(update={"artifact_max_bytes": 10})
    assert (await upload(client, boot["agent"], b"0123456789")).status_code == 201
    assert (await upload(client, boot["agent"], b"0123456789A")).status_code == 413


# --- authorization -----------------------------------------------------------


async def test_read_needs_the_right_on_the_artifacts_task(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    store: InMemoryContentStore,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = await create_task(client, boot["admin"])
    artifact = await stored_artifact(client, boot["agent"], task["id"], b"private")
    url = f"/api/v1/artifacts/{artifact['id']}/content"

    # Without artifacts.read at all.
    _, stranger = await create_agent_with_key(
        client, boot["admin"], name="stranger", permissions=["tasks.read"]
    )
    denied = await client.get(url, headers=auth(stranger))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"

    # With artifacts.read, but not on this task (a scoped PDP binding).
    asked = deny_on_task(monkeypatch, boot["otherId"], task["id"])
    denied = await client.get(url, headers=auth(boot["other"]))
    assert denied.status_code == 403
    assert (
        await client.get(f"/api/v1/artifacts/{artifact['id']}", headers=auth(boot["other"]))
    ).status_code == 403
    listed = await client.get(f"/api/v1/artifacts?taskId={task['id']}", headers=auth(boot["other"]))
    assert listed.status_code == 403
    assert f"task:{task['id']}" in asked
    # Writing onto that task is decided there as well.
    ref = (await uploaded(client, boot["other"], b"mine"))["contentRef"]
    write = await create_artifact(client, boot["other"], {"task": task["id"], "contentRef": ref})
    assert write.status_code == 403

    # Another tenant does not learn the artifact exists.
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    assert (await client.get(url, headers=auth(key_b))).status_code == 404

    assert len(await events_of(client, boot["admin"], "artifact.content_read")) == 0
    assert (await client.get(url, headers=auth(boot["agent"]))).status_code == 200


def deny_on_task(monkeypatch: pytest.MonkeyPatch, principal_id: str, task_id: str) -> list[str]:
    """Simulate a PDP that refuses ``principal_id`` everything about one task."""
    real: Callable[..., Any] = artifact_commands.authorize
    asked: list[str] = []

    async def authorize(ctx: Any, *any_of: Any, resource: Any = None, **kwargs: Any) -> None:
        if resource is not None:
            asked.append(resource.key)
        if (
            str(ctx.principal_id) == principal_id
            and resource is not None
            and resource.key == f"task:{task_id}"
        ):
            raise AuthorizationError(details={"resource": resource.key})
        await real(ctx, *any_of, resource=resource, **kwargs)

    monkeypatch.setattr(artifact_commands, "authorize", authorize)
    monkeypatch.setattr(execution_queries, "authorize", authorize)
    return asked


async def test_active_content_is_a_download(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    task = await create_task(client, boot["admin"])
    for media_type, disposition in (
        ("text/html; charset=utf-8", "attachment"),
        ("image/svg+xml", "attachment"),
        ("application/vnd.custom+xml", "attachment"),
        ("application/javascript", "attachment"),
        ("text/plain", "inline"),
        ("image/png", "inline"),
    ):
        ref = (await uploaded(client, boot["agent"], media_type.encode(), media_type=media_type))[
            "contentRef"
        ]
        artifact = (
            await create_artifact(
                client,
                boot["agent"],
                {"task": task["id"], "contentRef": ref, "name": "отчёт <1>.html"},
            )
        ).json()
        read = await client.get(
            f"/api/v1/artifacts/{artifact['id']}/content", headers=auth(boot["agent"])
        )
        assert read.status_code == 200
        assert read.headers["content-type"] == media_type
        assert read.headers["x-content-type-options"] == "nosniff"
        assert read.headers["content-disposition"] == (
            f"{disposition}; filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82%20%3C1%3E.html"
        )


# --- dedup and purge ---------------------------------------------------------


async def test_dedup_shares_one_object_and_purge_keeps_what_others_need(
    client: httpx.AsyncClient, boot: dict[str, Any], store: InMemoryContentStore
) -> None:
    task = await create_task(client, boot["admin"])
    data = b"the same bytes"
    key = object_key(uuid.UUID(boot["tenantId"]), hashlib.sha256(data).hexdigest())

    first = await stored_artifact(client, boot["agent"], task["id"], data)
    second_up = await uploaded(client, boot["other"], data)
    # The answer does not tell the second uploader the object existed.
    assert set(second_up) == {"contentRef", "sizeBytes", "mediaType", "sha256", "expiresAt"}
    second = (
        await create_artifact(
            client, boot["other"], {"task": task["id"], "contentRef": second_up["contentRef"]}
        )
    ).json()
    assert store.puts == 1
    assert list(store.objects) == [key]

    # Only an admin purges.
    agent_purge = await client.post(
        f"/api/v1/artifacts/{first['id']}:purge-content",
        json={"reason": "no"},
        headers=auth(boot["agent"]),
    )
    assert agent_purge.status_code == 403

    purged = await client.post(
        f"/api/v1/artifacts/{first['id']}:purge-content",
        json={"reason": "personal data, request #7"},
        headers=auth(boot["admin"]),
    )
    assert purged.status_code == 200, purged.text
    assert purged.json()["contentState"] == "purged"
    assert purged.json()["sha256"] == second["sha256"]
    # The other artifact still needs the object.
    assert key in store.objects
    read_first = await client.get(
        f"/api/v1/artifacts/{first['id']}/content", headers=auth(boot["agent"])
    )
    assert read_first.status_code == 410
    assert read_first.json()["error"]["code"] == "content_purged"
    read_second = await client.get(
        f"/api/v1/artifacts/{second['id']}/content", headers=auth(boot["other"])
    )
    assert read_second.content == data

    # A repeat is 200 with the same record and no second event.
    again = await client.post(
        f"/api/v1/artifacts/{first['id']}:purge-content",
        json={"reason": "again"},
        headers=auth(boot["admin"]),
    )
    assert again.status_code == 200
    assert again.json()["contentState"] == "purged"

    # The last stored artifact goes: so does the object — unless an upload is live.
    last = await client.post(
        f"/api/v1/artifacts/{second['id']}:purge-content",
        json={"reason": "retention"},
        headers=auth(boot["admin"]),
    )
    assert last.status_code == 200
    events = await events_of(client, boot["admin"], "artifact.content_purged")
    assert [e["payload"]["objectDeleted"] for e in reversed(events)] == [False, False]
    assert key in store.objects  # both uploads are still within their TTL

    none = (await create_artifact(client, boot["agent"], {"content": {"a": 1}})).json()
    conflict = await client.post(
        f"/api/v1/artifacts/{none['id']}:purge-content",
        json={"reason": "x"},
        headers=auth(boot["admin"]),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "content_not_stored"


async def test_purge_deletes_an_object_nothing_needs(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    store: InMemoryContentStore,
    sync_engine: Engine,
) -> None:
    task = await create_task(client, boot["admin"])
    artifact = await stored_artifact(client, boot["agent"], task["id"], b"only once")
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE artifact_contents SET expires_at = now() - interval '1 second'"))
    response = await client.post(
        f"/api/v1/artifacts/{artifact['id']}:purge-content",
        json={"reason": "retention"},
        headers=auth(boot["admin"]),
    )
    assert response.status_code == 200
    assert store.objects == {}
    event = (await events_of(client, boot["admin"], "artifact.content_purged"))[0]
    assert event["payload"]["objectDeleted"] is True
    assert event["payload"]["reason"] == "retention"
    assert event["payload"]["taskId"] == task["id"]


async def test_tenants_do_not_share_objects(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    store: InMemoryContentStore,
    sync_engine: Engine,
) -> None:
    tenant_b, key_b = make_tenant_directly(sync_engine, "tenant-b")
    data = b"same bytes, two tenants"
    a = (
        await create_artifact(
            client,
            boot["admin"],
            {"contentRef": (await uploaded(client, boot["admin"], data))["contentRef"]},
        )
    ).json()
    b = (
        await create_artifact(
            client, key_b, {"contentRef": (await uploaded(client, key_b, data))["contentRef"]}
        )
    ).json()
    assert len(store.objects) == 2
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE artifact_contents SET expires_at = now() - interval '1 second'"))
    await client.post(
        f"/api/v1/artifacts/{a['id']}:purge-content",
        json={"reason": "x"},
        headers=auth(boot["admin"]),
    )
    assert list(store.objects) == [object_key(uuid.UUID(tenant_b), a["sha256"])]
    read_b = await client.get(f"/api/v1/artifacts/{b['id']}/content", headers=auth(key_b))
    assert read_b.content == data


# --- sweep -------------------------------------------------------------------


async def test_sweep_drops_expired_unreferenced_uploads_only(
    app: FastAPI,
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    store: InMemoryContentStore,
    sync_engine: Engine,
    settings: Settings,
) -> None:
    task = await create_task(client, boot["admin"])
    tenant = uuid.UUID(boot["tenantId"])
    kept = await stored_artifact(client, boot["agent"], task["id"], b"referenced")
    shared = await uploaded(client, boot["agent"], b"referenced")  # same object, no artifact
    orphan = await uploaded(client, boot["agent"], b"orphan")
    fresh = await uploaded(client, boot["agent"], b"fresh")
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE artifact_contents SET expires_at = now() - interval '1 second'"
                " WHERE id <> :fresh"
            ),
            {"fresh": fresh["contentRef"].removeprefix("cref_")},
        )

    worker = Worker(settings, content_store=store)
    try:
        stats = await worker.run_once()
    finally:
        await worker.engine.dispose()
    assert stats["artifact_uploads_swept"] == 2  # the shared and the orphan upload

    with sync_engine.connect() as conn:
        left = set(conn.execute(text("SELECT sha256 FROM artifact_contents")).scalars())
    assert left == {kept["sha256"], fresh["sha256"]}
    assert object_key(tenant, kept["sha256"]) in store.objects
    assert object_key(tenant, shared["sha256"]) in store.objects
    assert object_key(tenant, orphan["sha256"]) not in store.objects
    assert object_key(tenant, fresh["sha256"]) in store.objects

    # A store that does not answer leaves the rows for the next pass.
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE artifact_contents SET expires_at = now() - interval '1 second'"))
    store.available = False
    assert await sweep_expired_uploads(app.state.session_factory, store) == 0
    store.available = True
    assert await sweep_expired_uploads(app.state.session_factory, store) == 1
    assert object_key(tenant, fresh["sha256"]) not in store.objects


# --- end to end over S3 ------------------------------------------------------

S3_ENDPOINT = os.environ.get("CP_TEST_S3_ENDPOINT_URL")


@pytest.mark.skipif(not S3_ENDPOINT, reason="CP_TEST_S3_ENDPOINT_URL not set (MinIO required)")
async def test_api_over_s3_creates_the_bucket_at_start_up(settings: Settings) -> None:
    s3_settings = settings.model_copy(
        update={
            "s3_endpoint_url": S3_ENDPOINT,
            "s3_bucket": f"cp-api-{uuid.uuid4().hex[:12]}",
            "s3_access_key_id": os.environ.get("CP_TEST_S3_ACCESS_KEY_ID", "minioadmin"),
            "s3_secret_access_key": os.environ.get("CP_TEST_S3_SECRET_ACCESS_KEY", "minioadmin"),
        }
    )
    application = create_app(s3_settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            admin = (await do_bootstrap(client))["apiKey"]["key"]
            data = os.urandom(2 * MIB + 5)
            artifact = await stored_artifact(
                client, admin, (await create_task(client, admin))["id"], data
            )
            read = await client.get(
                f"/api/v1/artifacts/{artifact['id']}/content", headers=auth(admin)
            )
            assert read.status_code == 200
            assert read.content == data
            purged = await client.post(
                f"/api/v1/artifacts/{artifact['id']}:purge-content",
                json={"reason": "contract"},
                headers=auth(admin),
            )
            assert purged.status_code == 200
