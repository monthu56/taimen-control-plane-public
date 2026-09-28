"""Package test, plan and apply in the core (CP-ADR-0074 §10, §11).

The body is the package as its files: the core parses YAML 1.2 itself so a
finding names the file and line. Test and plan write nothing; apply performs
exactly the plan whose hash it is given, or refuses ``409 plan_stale``.
``packages:test`` — :mod:`control_plane.application.commands.package_test`
(P013); plan and apply — :mod:`control_plane.application.commands.package_plan`
(P015).
"""

from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.calendars import calendar_request, spec_as_sent
from control_plane.api.v1.processes import RESPONSES
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PackageApplyOut,
    PackageApplyRequest,
    PackagePlanOut,
    PackagePlanRequest,
    PackageTestOut,
    PackageTestRequest,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import package_plan
from control_plane.application.commands.package_test import run_package_tests
from control_plane.domain.calendar import Calendar, CalendarError
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.domain.package_source import PackageObject
from control_plane.domain.process_definition import Problem
from control_plane.infrastructure.context_provider import GraphProvider

router = APIRouter(tags=["packages"])


def calendar_spec(obj: PackageObject) -> tuple[dict[str, Any] | None, list[Problem]]:
    """A package object of kind Calendar as ``POST /calendars`` takes its spec, or its findings."""
    try:
        payload = calendar_request({"kind": obj.kind, "key": obj.key, "spec": obj.spec})
    except ValidationError as exc:
        errors = (exc.details or {}).get("errors") or [{"path": "/spec", "message": exc.message}]
        return None, [
            Problem("invalid_calendar", "error", str(e["path"]), str(e["message"])) for e in errors
        ]
    spec = spec_as_sent(payload)
    try:
        Calendar.from_spec(spec)
    except CalendarError as exc:
        return None, [Problem(exc.code, "error", exc.path or "/spec", exc.message)]
    return spec, []


def check_calendar(obj: PackageObject) -> tuple[Calendar | None, list[Problem]]:
    """A package object of kind Calendar: its shape (``CalendarSpec``), then the calendar itself."""
    spec, problems = calendar_spec(obj)
    return (Calendar.from_spec(spec) if spec is not None else None), problems


@router.post(
    "/packages:test",
    response_model=PackageTestOut,
    responses=RESPONSES,
    summary="Check a package and run its tests in a sandbox; nothing is written",
)
async def package_tests(
    payload: PackageTestRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    check_only: bool = Query(
        default=False, alias="checkOnly", description="Check the package, run no test"
    ),
) -> JSONResponse:
    await authorize(ctx, Permission.PACKAGES_TEST)
    report = await run_package_tests(
        session_factory,
        ctx,
        settings,
        cast(GraphProvider | None, getattr(request.app.state, "context_provider", None)),
        files=[(item.path, item.content) for item in payload.package.files],
        tests=payload.tests,
        workspace_id=payload.workspace_id,
        check_only=check_only,
        check_calendar=check_calendar,
    )
    body = PackageTestOut.model_validate(report.out()).model_dump(mode="json", by_alias=True)
    return JSONResponse(body)


@router.post(
    "/packages:plan",
    response_model=PackagePlanOut,
    responses=ERROR_RESPONSES,
    summary="Plan applying a package: structural and behavioural diff, open instances, hash",
)
async def plan_package(
    payload: PackagePlanRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.PACKAGES_PLAN)
    plan = await package_plan.plan_package(
        session_factory,
        ctx,
        settings,
        cast(GraphProvider | None, getattr(request.app.state, "context_provider", None)),
        files=[(item.path, item.content) for item in payload.package.files],
        workspace_id=payload.workspace_id,
        replay_limit=payload.replay_limit,
        overwrite=payload.overwrite_console,
        calendar_spec=calendar_spec,
    )
    body = PackagePlanOut.model_validate(plan.out()).model_dump(mode="json", by_alias=True)
    return JSONResponse(body)


@router.post(
    "/packages:apply",
    response_model=PackageApplyOut,
    responses=ERROR_RESPONSES,
    summary="Apply exactly the plan with this hash; the catalog changed since — 409 plan_stale",
)
async def apply_package(
    payload: PackageApplyRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    # packages.plan here; each change is checked again against the right of
    # its kind (processes.write, calendars.write) when it is applied.
    await authorize(ctx, Permission.PACKAGES_PLAN)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        applied = await package_plan.apply_package(
            db,
            ctx,
            files=[(item.path, item.content) for item in payload.package.files],
            expected_hash=payload.plan_hash,
            workspace_id=payload.workspace_id,
            overwrite=payload.overwrite_console,
            calendar_spec=calendar_spec,
        )
        return 200, PackageApplyOut.model_validate(applied).model_dump(mode="json", by_alias=True)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(),
        executor=executor,
    )
