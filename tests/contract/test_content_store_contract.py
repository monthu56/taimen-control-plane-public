"""Contract of the ``ContentStore`` port (CP-ADR-0072 §3).

The same assertions run against the in-memory adapter (always) and the S3
adapter over a real S3-compatible service when one is configured — MinIO in
CI:

    CP_TEST_S3_ENDPOINT_URL=http://127.0.0.1:9000
    CP_TEST_S3_ACCESS_KEY_ID=minioadmin
    CP_TEST_S3_SECRET_ACCESS_KEY=minioadmin

Each run works in a bucket of its own, which ``ensure_bucket`` creates.
"""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from control_plane.infrastructure.content_store import (
    ContentObjectMissing,
    ContentStore,
    ContentStoreUnavailable,
    InMemoryContentStore,
    object_key,
    spool,
)
from control_plane.infrastructure.content_store.s3 import S3ContentStore

S3_ENDPOINT = os.environ.get("CP_TEST_S3_ENDPOINT_URL")


def s3_store(endpoint: str, bucket: str) -> S3ContentStore:
    return S3ContentStore(
        endpoint_url=endpoint,
        bucket=bucket,
        region=os.environ.get("CP_TEST_S3_REGION", "us-east-1"),
        access_key_id=os.environ.get("CP_TEST_S3_ACCESS_KEY_ID", "minioadmin"),
        secret_access_key=os.environ.get("CP_TEST_S3_SECRET_ACCESS_KEY", "minioadmin"),
        connect_timeout=2.0,
        read_timeout=10.0,
    )


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "s3",
            marks=pytest.mark.skipif(
                not S3_ENDPOINT, reason="CP_TEST_S3_ENDPOINT_URL not set (MinIO required)"
            ),
        ),
    ]
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ContentStore]:
    if request.param == "memory":
        yield InMemoryContentStore()
        return
    assert S3_ENDPOINT is not None
    instance = s3_store(S3_ENDPOINT, f"cp-contract-{uuid.uuid4().hex[:12]}")
    yield instance
    await instance.aclose()


async def _file(tmp_path: Path, data: bytes) -> tuple[Path, str, int]:
    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(data), 65536):
            yield data[offset : offset + 65536]

    spooled = await spool(chunks(), limit=len(data))
    target = tmp_path / spooled.path.name
    spooled.path.rename(target)
    return target, spooled.sha256, spooled.size


async def _read(store: ContentStore, key: str) -> bytes:
    stream = await store.open(key)
    parts = [chunk async for chunk in stream.chunks()]
    return b"".join(parts)


async def test_round_trip(store: ContentStore, tmp_path: Path) -> None:
    await store.ensure_bucket()
    await store.ensure_bucket()  # idempotent
    data = os.urandom(3 * 1024 * 1024 + 17)  # several read chunks
    path, sha, size = await _file(tmp_path, data)
    assert sha == hashlib.sha256(data).hexdigest()
    key = object_key(uuid.uuid4(), sha)

    assert await store.exists(key) is False
    await store.put(key, path, size)
    assert await store.exists(key) is True
    stream = await store.open(key)
    assert stream.size == size
    await stream.aclose()
    assert await _read(store, key) == data

    await store.delete(key)
    assert await store.exists(key) is False
    await store.delete(key)  # absent: not an error
    with pytest.raises(ContentObjectMissing):
        await store.open(key)


async def test_empty_object(store: ContentStore, tmp_path: Path) -> None:
    await store.ensure_bucket()
    path, sha, size = await _file(tmp_path, b"")
    key = object_key(uuid.uuid4(), sha)
    await store.put(key, path, size)
    assert (await store.open(key)).size == 0
    assert await _read(store, key) == b""


async def test_tenant_prefixes_keep_objects_apart(store: ContentStore, tmp_path: Path) -> None:
    await store.ensure_bucket()
    path, sha, size = await _file(tmp_path, b"shared bytes")
    first, second = object_key(uuid.uuid4(), sha), object_key(uuid.uuid4(), sha)
    await store.put(first, path, size)
    await store.put(second, path, size)
    await store.delete(first)
    assert await store.exists(second) is True
    assert await _read(store, second) == b"shared bytes"


async def test_unreachable_s3_is_unavailable(tmp_path: Path) -> None:
    """A service that does not answer is an outage (503), never a crash."""
    store = s3_store("http://127.0.0.1:9", "cp-down")
    with pytest.raises(ContentStoreUnavailable):
        await store.ensure_bucket()
    with pytest.raises(ContentStoreUnavailable):
        await store.exists("tenants/x/sha256/y")
    path, _, size = await _file(tmp_path, b"x")
    with pytest.raises(ContentStoreUnavailable):
        await store.put("tenants/x/sha256/y", path, size)
    await store.aclose()
