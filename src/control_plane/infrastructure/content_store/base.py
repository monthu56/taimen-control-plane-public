"""The ``ContentStore`` port: where the bytes of artifacts live (CP-ADR-0072 §3).

The core never keeps artifact bytes in PostgreSQL or in process memory. An
uploaded file is spooled to a temporary file first (``spool``), then handed to
the store whole; a read streams the object back chunk by chunk.

Objects are addressed by content inside a tenant:
``tenants/<tenantId>/sha256/<hex>`` (``object_key``). The store knows nothing
about artifacts, uploads or who may read what — that bookkeeping is the
application's (``artifact_contents``, ``artifacts``).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

# Size of one chunk read from a stored object while streaming it out.
READ_CHUNK_BYTES = 256 * 1024


class ContentStoreUnavailable(Exception):
    """The store is not configured or does not answer; the caller gets 503."""


class ContentObjectMissing(Exception):
    """The object a record points to is not in the store."""


def object_key(tenant_id: uuid.UUID, sha256: str) -> str:
    """Object key of the content ``sha256`` in ``tenant_id``'s prefix."""
    return f"tenants/{tenant_id}/sha256/{sha256}"


class ContentStream(ABC):
    """An opened object: its size and its bytes, read lazily."""

    size: int

    @abstractmethod
    def chunks(self) -> AsyncIterator[bytes]:
        """The bytes of the object, chunk by chunk; closes the stream at the end."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release the stream without reading it to the end."""


class ContentStore(ABC):
    """Byte storage of artifact content, addressed by object key."""

    @abstractmethod
    async def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist yet (called at start-up)."""

    @abstractmethod
    async def put(self, key: str, path: Path, size: int) -> None:
        """Store the finished file at ``path`` under ``key`` (overwrites)."""

    @abstractmethod
    async def open(self, key: str) -> ContentStream:
        """Open the object for reading; ``ContentObjectMissing`` if absent."""

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the object; removing an absent object is not an error."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, most stores hold nothing
        """Release clients held by the store."""
