"""``package`` of catalog objects in lists and cards, ``?package=<key>`` of lists.

CP-ADR-0074 §11, amendment TASK-000904 (:mod:`control_plane.domain.package_links`).
A list reads the links of its page in one query (:func:`attach_packages`); a
filter is an ``EXISTS`` over ``package_objects`` (:func:`in_package`), so the
page and its cursor stay those of the list.
"""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from control_plane.domain.package_links import PackageLink
from control_plane.infrastructure.db.models import PackageObject


def link_of(row: PackageObject) -> PackageLink:
    return PackageLink(
        key=row.package_key,
        version=row.package_version,
        install_hash=row.plan_hash,
        installed_at=row.applied_at,
    )


async def package_links(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, keys: Iterable[str]
) -> dict[str, PackageLink]:
    """The links of ``keys`` of ``kind``; a key without one was created by hand."""
    wanted = sorted(set(keys))
    if not wanted:
        return {}
    rows = await session.scalars(
        select(PackageObject).where(
            PackageObject.tenant_id == tenant_id,
            PackageObject.kind == kind,
            PackageObject.key.in_(wanted),
        )
    )
    return {row.key: link_of(row) for row in rows}


async def attach_packages(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    items: list[dict[str, Any]],
    *,
    key_field: str = "key",
) -> list[dict[str, Any]]:
    """Set ``package`` of each response body in place: the link, or ``null``.

    A role of a workspace is never a package's (packages bring tenant roles),
    whatever its slug.
    """

    def eligible(item: dict[str, Any]) -> bool:
        return kind != "Role" or item.get("workspaceId") is None

    keys = (item[key_field] for item in items if eligible(item))
    links = await package_links(session, tenant_id, kind, keys)
    for item in items:
        link = links.get(item[key_field]) if eligible(item) else None
        item["package"] = link.out() if link is not None else None
    return items


async def attach_package(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    item: dict[str, Any],
    *,
    key_field: str = "key",
) -> dict[str, Any]:
    await attach_packages(session, tenant_id, kind, [item], key_field=key_field)
    return item


def in_package(
    kind: str,
    tenant_column: InstrumentedAttribute[uuid.UUID],
    key_column: InstrumentedAttribute[str],
    package: str,
) -> ColumnElement[bool]:
    """Rows of ``kind`` whose key the package ``package`` installed."""
    return exists().where(
        and_(
            PackageObject.tenant_id == tenant_column,
            PackageObject.kind == kind,
            PackageObject.key == key_column,
            PackageObject.package_key == package,
        )
    )
