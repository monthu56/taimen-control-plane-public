"""Working-day calendars (CP-ADR-0074 §9).

A calendar is tenant data every process reads through ``cal.*``: a new
version only when the canonical hash of its spec differs. Reading needs
authentication alone; the calendar holds no secret and no personal data.
"""

from typing import Any

import pydantic
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    CalendarOut,
    CalendarPublishRequest,
    PageOut,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands import calendars as commands
from control_plane.application.common import decode_cursor, encode_cursor
from control_plane.application.queries.lists import clamp_limit
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError

router = APIRouter(tags=["calendars"])

CATALOG_KIND = "Calendar"


def calendar_body(view: commands.CalendarView) -> dict[str, Any]:
    row = view.row
    return CalendarOut(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        version=row.version,
        latest_version=view.latest_version,
        calendar_hash=row.calendar_hash,
        spec=row.spec,
        provisional_years=sorted(
            entry["year"] for entry in row.spec["years"] if entry.get("provisional")
        ),
        created_by=row.created_by,
        created_at=row.created_at,
    ).model_dump(mode="json", by_alias=True)


def spec_as_sent(payload: CalendarPublishRequest) -> dict[str, Any]:
    """The spec without defaults filled in: that is what a version stores and hashes."""
    return payload.spec.model_dump(mode="json", by_alias=True, exclude_unset=True)


def calendar_request(document: dict[str, Any]) -> CalendarPublishRequest:
    """The publish request of a package file of kind ``Calendar``.

    The object is the catalog document ``{apiVersion, kind, key, spec}``
    whose envelope the package check has already matched against the catalog
    schema; the installer applies it exactly as ``POST /calendars {key,
    spec}``, under ``calendars.write`` of whoever applies the package
    (CP-ADR-0074 §11).
    """
    if document.get("kind") != CATALOG_KIND:
        raise ValidationError(
            "invalid_calendar",
            f"Not a catalog object of kind {CATALOG_KIND}",
            details={"kind": document.get("kind")},
        )
    try:
        return CalendarPublishRequest.model_validate(
            {"key": document.get("key"), "spec": document.get("spec")}
        )
    except pydantic.ValidationError as exc:
        raise ValidationError(
            "invalid_calendar",
            "The calendar object does not match $defs.calendarSpec",
            details={
                "errors": [
                    {"path": "/" + "/".join(map(str, error["loc"])), "message": error["msg"]}
                    for error in exc.errors()
                ]
            },
        ) from exc


async def install_calendar(
    db: AsyncSession, ctx: AuthContext, document: dict[str, Any]
) -> commands.CalendarView:
    """Install a package object of kind ``Calendar`` in the caller's transaction."""
    payload = calendar_request(document)
    return await commands.publish_calendar(db, ctx, key=payload.key, spec=spec_as_sent(payload))


@router.post(
    "/calendars",
    response_model=CalendarOut,
    status_code=201,
    responses={**ERROR_RESPONSES, 200: {"model": CalendarOut, "description": "Spec unchanged"}},
    summary="Publish a calendar: a new version only when its canonical hash differs",
)
async def publish_calendar(
    payload: CalendarPublishRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.CALENDARS_WRITE)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await commands.publish_calendar(db, ctx, key=payload.key, spec=spec_as_sent(payload))
        return (201 if view.created else 200), calendar_body(view)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/calendars", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_calendars(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    # Authentication is the whole check (module docstring).
    after_key: str | None = None
    if cursor is not None:
        after_key = decode_cursor(cursor).get("k")
        if not isinstance(after_key, str):
            raise ValidationError("invalid_cursor", "Malformed pagination cursor")
    views, next_key = await commands.list_calendars(
        db, ctx, limit=clamp_limit(limit), after_key=after_key
    )
    next_cursor = encode_cursor({"k": next_key}) if next_key is not None else None
    return JSONResponse(page_body([calendar_body(view) for view in views], next_cursor))


@router.get(
    "/calendars/{ref}",
    response_model=CalendarOut,
    responses=ERROR_RESPONSES,
    summary="A calendar by key (latest version) or key@version",
)
async def get_calendar(ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    return JSONResponse(calendar_body(await commands.resolve_calendar(db, ctx, ref)))
