"""In-memory ``ContentStore`` for tests: a dict of bytes, switchable off."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from control_plane.infrastructure.content_store.base import (
    READ_CHUNK_BYTES,
    ContentObjectMissing,
    ContentStore,
    ContentStoreUnavailable,
    ContentStream,
)


class _BytesStream(ContentStream):
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.size = len(data)

    async def chunks(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._data), READ_CHUNK_BYTES):
            yield self._data[offset : offset + READ_CHUNK_BYTES]

    async def aclose(self) -> None:
        return None


class InMemoryContentStore(ContentStore):
    """Objects in a dict; ``available = False`` simulates an outage."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.available = True
        self.bucket_ready = False
        self.puts = 0

    def _check(self) -> None:
        if not self.available:
            raise ContentStoreUnavailable("in-memory store switched off")

    async def ensure_bucket(self) -> None:
        self._check()
        self.bucket_ready = True

    async def put(self, key: str, path: Path, size: int) -> None:
        self._check()
        data = await asyncio.to_thread(path.read_bytes)
        if len(data) != size:
            raise ValueError(f"spooled file changed: {len(data)} != {size}")
        self.objects[key] = data
        self.puts += 1

    async def open(self, key: str) -> ContentStream:
        self._check()
        if key not in self.objects:
            raise ContentObjectMissing(key)
        return _BytesStream(self.objects[key])

    async def exists(self, key: str) -> bool:
        self._check()
        return key in self.objects

    async def delete(self, key: str) -> None:
        self._check()
        self.objects.pop(key, None)
