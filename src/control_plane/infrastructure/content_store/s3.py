"""S3-compatible ``ContentStore`` (MinIO, AWS S3, any s3v4 service).

``boto3`` is synchronous: every call runs in a worker thread
(``asyncio.to_thread``) so the event loop never waits on the network. A
network failure or an unexpected answer of the service is
``ContentStoreUnavailable`` — the API maps it to 503, the record side keeps
working (CP-ADR-0072 §3).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from control_plane import sandbox
from control_plane.infrastructure.content_store.base import (
    READ_CHUNK_BYTES,
    ContentObjectMissing,
    ContentStore,
    ContentStoreUnavailable,
    ContentStream,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = logging.getLogger(__name__)

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_NO_BUCKET_CODES = frozenset({"404", "NoSuchBucket", "NotFound"})


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class _S3Stream(ContentStream):
    def __init__(self, body: Any, size: int) -> None:
        self._body = body
        self.size = size

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    chunk: bytes = await asyncio.to_thread(self._body.read, READ_CHUNK_BYTES)
                except (BotoCoreError, OSError) as exc:
                    # Headers are gone by now: the client sees a cut response.
                    raise ContentStoreUnavailable(str(exc)) from exc
                if not chunk:
                    return
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._body.close)


class S3ContentStore(ContentStore):
    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        region: str,
        access_key_id: str | None,
        secret_access_key: str | None,
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
    ) -> None:
        self.bucket = bucket
        self._config = Config(
            signature_version="s3v4",
            region_name=region,
            # A custom endpoint (MinIO and the like) rarely has per-bucket DNS.
            s3={"addressing_style": "path"},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": 2, "mode": "standard"},
            # Compatibility with S3 implementations that do not know the
            # newer default integrity headers.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client: S3Client | None = None
        self._client_lock = threading.Lock()
        self._bucket_ready = False

    def _s3(self) -> S3Client:
        # boto3 clients are thread-safe once built, building one is not.
        with self._client_lock:
            if self._client is None:
                self._client = boto3.session.Session().client(
                    "s3",
                    endpoint_url=self._endpoint_url,
                    aws_access_key_id=self._access_key_id,
                    aws_secret_access_key=self._secret_access_key,
                    config=self._config,
                )
            return self._client

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        sandbox.refuse_outgoing("content_store")
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ClientError:
            raise
        except (BotoCoreError, OSError) as exc:
            raise ContentStoreUnavailable(str(exc)) from exc

    async def ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        s3 = self._s3()
        try:
            await self._call(s3.head_bucket, Bucket=self.bucket)
        except ClientError as exc:
            if _code(exc) not in _NO_BUCKET_CODES:
                raise ContentStoreUnavailable(f"head_bucket: {_code(exc)}") from exc
            try:
                await self._call(s3.create_bucket, Bucket=self.bucket)
            except ClientError as create_exc:
                # Another replica may have created it in between.
                if _code(create_exc) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise ContentStoreUnavailable(
                        f"create_bucket: {_code(create_exc)}"
                    ) from create_exc
            logger.info("content store bucket created", extra={"bucket": self.bucket})
        self._bucket_ready = True

    async def put(self, key: str, path: Path, size: int) -> None:
        await self.ensure_bucket()
        s3 = self._s3()

        def upload() -> None:
            with path.open("rb") as handle:
                s3.put_object(Bucket=self.bucket, Key=key, Body=handle, ContentLength=size)

        try:
            await self._call(upload)
        except ClientError as exc:
            raise ContentStoreUnavailable(f"put_object: {_code(exc)}") from exc

    async def open(self, key: str) -> ContentStream:
        await self.ensure_bucket()
        try:
            response = await self._call(self._s3().get_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _code(exc) in _MISSING_CODES:
                raise ContentObjectMissing(key) from exc
            raise ContentStoreUnavailable(f"get_object: {_code(exc)}") from exc
        return _S3Stream(response["Body"], int(response["ContentLength"]))

    async def exists(self, key: str) -> bool:
        await self.ensure_bucket()
        try:
            await self._call(self._s3().head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _code(exc) in _MISSING_CODES:
                return False
            raise ContentStoreUnavailable(f"head_object: {_code(exc)}") from exc
        return True

    async def delete(self, key: str) -> None:
        await self.ensure_bucket()
        try:
            await self._call(self._s3().delete_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _code(exc) not in _MISSING_CODES:
                raise ContentStoreUnavailable(f"delete_object: {_code(exc)}") from exc

    async def aclose(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.close)
