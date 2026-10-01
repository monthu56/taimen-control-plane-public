"""Processes and calendars out of use (CP-ADR-0074, amendment 2026-09-29, Zh1).

Versions are immutable, so retirement belongs to the key: one row of
``catalog_retirements`` per retired ``(tenant, kind, key)``. The routes
``:retire`` of a process and a calendar and a package renaming a key away
write the row; a new version of the key deletes it — retirement is a decision
about using the key, not a ban on its name.
"""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.domain.errors import DependencyUnavailableError
from control_plane.infrastructure.db.models import CatalogRetirement

PROCESS = "Process"
CALENDAR = "Calendar"
# The reason of a key a package renamed away, followed by the package key; the
# migration that introduced the table writes the same.
RENAMED_BY = "renamed by package "
# The advisory lock of a key: publication and retirement take it exclusively;
# what must not act on a key while it is being retired takes it shared.
_LOCKS = {PROCESS: "cp:process", CALENDAR: "cp:calendar"}


def _lock_id(kind: str, tenant_id: uuid.UUID, key: str) -> Any:
    return func.hashtextextended(f"{_LOCKS[kind]}:{tenant_id}:{key}", 0)


async def lock_key(session: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str) -> None:
    """Serialize publication and retirement of one (tenant, kind, key)."""
    await session.execute(select(func.pg_advisory_xact_lock(_lock_id(kind, tenant_id, key))))


async def lock_keys(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, keys: Iterable[str]
) -> None:
    """Hold several keys of a kind exclusively, sorted: one order for every holder of many."""
    for key in sorted(set(keys)):
        await lock_key(session, tenant_id, kind, key)


async def share_keys(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, keys: Iterable[str]
) -> None:
    """Wait out a publication or retirement of the keys in flight, then keep them from one.

    Taken before reading whether a key is retired, it makes the reading hold
    until the transaction ends: a start does not slip past a retirement that
    has counted the open instances, a process version does not name a calendar
    a retirement found unneeded.
    """
    for key in sorted(set(keys)):
        await session.execute(
            select(func.pg_advisory_xact_lock_shared(_lock_id(kind, tenant_id, key)))
        )


KEY_BUSY = "catalog_key_busy"


def key_busy(kind: str, key: str) -> DependencyUnavailableError:
    return DependencyUnavailableError(
        f"{kind} {key!r} is being published or retired: try again",
        code=KEY_BUSY,
        details={"kind": kind, "key": key},
    )


def is_key_busy(exc: BaseException) -> bool:
    """A refusal of :func:`share_keys_now`: expected while an apply runs, not a failure."""
    return isinstance(exc, DependencyUnavailableError) and exc.code == KEY_BUSY


async def share_keys_now(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, keys: Iterable[str]
) -> None:
    """:func:`share_keys` without waiting: a key held exclusively is ``catalog_key_busy``.

    For a transaction that goes on holding what it took before — the process
    engine's batch holds the keys of the children it has started. Waiting
    there closes a cycle with an apply that holds this key and waits for one
    the batch holds; refused, the batch rolls back, lets its keys go and is
    retried (``DependencyUnavailableError`` passes every savepoint of a step).
    """
    for key in sorted(set(keys)):
        got = await session.scalar(
            select(func.pg_try_advisory_xact_lock_shared(_lock_id(kind, tenant_id, key)))
        )
        if not got:
            raise key_busy(kind, key)


async def retirement(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str
) -> CatalogRetirement | None:
    row: CatalogRetirement | None = await session.get(CatalogRetirement, (tenant_id, kind, key))
    return row


async def retirements(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, keys: Iterable[str] | None = None
) -> dict[str, CatalogRetirement]:
    """The retired keys of a kind (of ``keys`` when given) with their rows."""
    stmt = select(CatalogRetirement).where(
        CatalogRetirement.tenant_id == tenant_id, CatalogRetirement.kind == kind
    )
    if keys is not None:
        wanted = sorted(set(keys))
        if not wanted:
            return {}
        stmt = stmt.where(CatalogRetirement.key.in_(wanted))
    return {row.key: row for row in await session.scalars(stmt)}


async def retired_keys(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, keys: Iterable[str] | None = None
) -> frozenset[str]:
    return frozenset(await retirements(session, tenant_id, kind, keys))


async def retire_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    key: str,
    *,
    by: uuid.UUID,
    reason: str,
    at: Any,
) -> CatalogRetirement:
    """Mark the key retired; a key already retired keeps its first retirement."""
    await session.execute(
        insert(CatalogRetirement)
        .values(
            tenant_id=tenant_id, kind=kind, key=key, retired_at=at, retired_by=by, reason=reason
        )
        .on_conflict_do_nothing()
    )
    row = await session.get(CatalogRetirement, (tenant_id, kind, key), populate_existing=True)
    assert row is not None
    return row


async def restore_key(session: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str) -> None:
    """A new version brings the key back into use."""
    await session.execute(
        delete(CatalogRetirement).where(
            CatalogRetirement.tenant_id == tenant_id,
            CatalogRetirement.kind == kind,
            CatalogRetirement.key == key,
        )
    )


def is_retired(kind: str, tenant_id: Any, key: Any) -> Any:
    """``EXISTS`` a retirement of the key: the ``?status=`` filter of the lists."""
    return (
        select(CatalogRetirement.key)
        .where(
            CatalogRetirement.tenant_id == tenant_id,
            CatalogRetirement.kind == kind,
            CatalogRetirement.key == key,
        )
        .exists()
    )


def retirement_out(row: CatalogRetirement | None) -> dict[str, Any] | None:
    """``RetirementOut`` of a response, ``None`` for a key in use."""
    if row is None:
        return None
    return {"at": row.retired_at, "by": row.retired_by, "reason": row.reason}
