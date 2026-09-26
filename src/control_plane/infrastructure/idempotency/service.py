"""Idempotent execution of mutating commands.

Protocol:
- First request with a given ``Idempotency-Key`` inserts a *pending* record
  (``INSERT ... ON CONFLICT DO NOTHING``), executes the command, and stores
  the response **in the same transaction** as the command itself — the saved
  response is exactly as atomic as the state change.
- A concurrent duplicate loses the insert race and waits for the winner's
  response, then replays it.
- The same key with a different request hash is a ``409 idempotency_key_reused``.
- If the winner fails, its pending record is removed so a retry can execute.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.common import utcnow
from control_plane.config import Settings
from control_plane.domain.errors import ConflictError
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import IdempotencyKey

Executor = Callable[[AsyncSession], Awaitable[tuple[int, dict[str, Any]]]]


def request_fingerprint(method: str, path: str, canonical_body: str) -> str:
    return hashlib.sha256(f"{method}\n{path}\n{canonical_body}".encode()).hexdigest()


async def _try_acquire(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    key: str,
    method: str,
    path: str,
    request_hash: str,
    pending_ttl_seconds: int,
) -> bool:
    now = utcnow()
    # Expired leftovers must not block a fresh execution.
    await session.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.tenant_id == tenant_id,
            IdempotencyKey.key == key,
            IdempotencyKey.expires_at <= now,
        )
    )
    stmt = (
        pg_insert(IdempotencyKey)
        .values(
            tenant_id=tenant_id,
            key=key,
            principal_id=principal_id,
            request_method=method,
            request_path=path,
            request_hash=request_hash,
            response_status=None,
            response_body=None,
            created_at=now,
            # Pending records live briefly: a crashed executor must not wedge
            # the key for the full retention TTL. The successful UPDATE below
            # extends expires_at to the full TTL.
            expires_at=now + timedelta(seconds=pending_ttl_seconds),
        )
        .on_conflict_do_nothing(index_elements=[IdempotencyKey.tenant_id, IdempotencyKey.key])
        .returning(IdempotencyKey.key)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    return inserted is not None


async def run_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    key: str,
    method: str,
    path: str,
    request_hash: str,
    executor: Executor,
    sensitive_fields: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any], bool]:
    """Execute (or replay) a command under an idempotency key.

    Returns ``(status_code, response_body, replayed)``.

    ``sensitive_fields`` are one-time secrets in the response (e.g. the full
    API key): they are nulled out in the STORED copy, so a replay returns the
    logical result without re-disclosing the secret, and the secret never
    rests in the database.
    """
    deadline = asyncio.get_running_loop().time() + settings.idempotency_wait_timeout_seconds
    backoff = 0.05

    while True:
        async with transaction(session_factory) as session:
            acquired = await _try_acquire(
                session,
                tenant_id=tenant_id,
                principal_id=principal_id,
                key=key,
                method=method,
                path=path,
                request_hash=request_hash,
                pending_ttl_seconds=settings.idempotency_pending_ttl_seconds,
            )

        if acquired:
            try:
                async with transaction(session_factory) as session:
                    status_code, body = await executor(session)
                    stored_body = body
                    if sensitive_fields:
                        stored_body = {
                            **body,
                            **{f: None for f in sensitive_fields if f in body},
                        }
                    await session.execute(
                        update(IdempotencyKey)
                        .where(
                            IdempotencyKey.tenant_id == tenant_id,
                            IdempotencyKey.key == key,
                        )
                        .values(
                            response_status=status_code,
                            response_body=stored_body,
                            expires_at=utcnow()
                            + timedelta(seconds=settings.idempotency_ttl_seconds),
                        )
                    )
            except BaseException:
                # The command failed and rolled back; free the key for retries.
                async with transaction(session_factory) as session:
                    await session.execute(
                        delete(IdempotencyKey).where(
                            IdempotencyKey.tenant_id == tenant_id,
                            IdempotencyKey.key == key,
                            IdempotencyKey.response_status.is_(None),
                        )
                    )
                raise
            return status_code, body, False

        async with session_factory() as session:
            record = await session.scalar(
                select(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == tenant_id,
                    IdempotencyKey.key == key,
                )
            )
        if record is not None:
            # A key belongs to the principal that first used it: replaying
            # another principal's response would bypass authorization.
            if record.request_hash != request_hash or record.principal_id != principal_id:
                raise ConflictError(
                    "idempotency_key_reused",
                    "Idempotency key was already used with a different request",
                    details={"key": key},
                )
            if record.response_status is not None:
                return record.response_status, record.response_body or {}, True

        # Pending (or just deleted by a failed winner): wait and re-check.
        if asyncio.get_running_loop().time() >= deadline:
            raise ConflictError(
                "idempotency_in_flight",
                "An identical request is still being processed; retry later",
                details={"key": key},
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 0.5)
