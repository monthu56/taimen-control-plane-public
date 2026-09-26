"""Artifact content store boundary (CP-ADR-0072 §3): optional, S3-compatible."""

from control_plane.config import Settings
from control_plane.infrastructure.content_store.base import (
    ContentObjectMissing,
    ContentStore,
    ContentStoreUnavailable,
    ContentStream,
    object_key,
)
from control_plane.infrastructure.content_store.memory import InMemoryContentStore
from control_plane.infrastructure.content_store.spool import (
    SpooledFile,
    SpoolLimitExceeded,
    spool,
)


def build_content_store(settings: Settings) -> ContentStore | None:
    """None without ``CP_S3_ENDPOINT_URL``: content is off, records still work."""
    if not settings.s3_endpoint_url:
        return None
    from control_plane.infrastructure.content_store.s3 import S3ContentStore

    return S3ContentStore(
        endpoint_url=settings.s3_endpoint_url,
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        connect_timeout=settings.s3_connect_timeout_seconds,
        read_timeout=settings.s3_read_timeout_seconds,
    )


__all__ = [
    "ContentObjectMissing",
    "ContentStore",
    "ContentStoreUnavailable",
    "ContentStream",
    "InMemoryContentStore",
    "SpoolLimitExceeded",
    "SpooledFile",
    "build_content_store",
    "object_key",
    "spool",
]
