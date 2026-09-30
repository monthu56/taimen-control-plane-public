"""Work rule endpoints: rules as tenant data, and their history (CP-ADR-0063).

Optimistic concurrency as for goals: GET returns ``ETag: "rule-<version>"``
and PATCH requires ``If-Match``. ``:enable`` / ``:disable`` repeat harmlessly;
``DELETE`` archives (the history stays, the key becomes free).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    PageOut,
    RuleCreateRequest,
    RuleEvaluationOut,
    RuleOut,
    RuleUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import as_no_content, execute_write
from control_plane.application.commands import work_rules as commands
from control_plane.application.commands.work_rules import _UNSET
from control_plane.application.queries import work_rules as queries
from control_plane.application.queries.package_links import attach_package, attach_packages
from control_plane.domain.errors import ValidationError
from control_plane.domain.work_rules import RuleStatus

router = APIRouter(tags=["rules"])

_RULE_ENTITY = "rule"

# Null on these is never "clear it"; condition, interpretation, goalId and
# identity are.
_NON_NULLABLE_PATCH_FIELDS = ("description", "trigger", "action")


@router.post(
    "/rules",
    response_model=RuleOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Write a rule that derives work from observed facts",
)
async def create_rule(
    payload: RuleCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        rule = await commands.create_rule(
            db,
            ctx,
            key=payload.key,
            description=payload.description,
            workspace_id=payload.workspace_id,
            goal_id=payload.goal_id,
            trigger=payload.trigger,
            condition=payload.condition,
            interpretation=payload.interpretation,
            action=payload.action,
            status=payload.status,
            identity=payload.identity.model_dump() if payload.identity else None,
        )
        return 201, await attach_package(db, ctx.tenant_id, "WorkRule", dump(RuleOut, rule))

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/rules", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_rules(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: str | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    key: str | None = Query(default=None),
    trigger_kind: str | None = Query(default=None, alias="triggerKind"),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    page = await queries.list_rules(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        status=status,
        workspace_id=workspace_id,
        key=key,
        trigger_kind=trigger_kind,
        package=package,
    )
    items = [dump(RuleOut, r) for r in page.items]
    await attach_packages(db, ctx.tenant_id, "WorkRule", items)
    return JSONResponse(page_body(items, page.next_cursor))


@router.get("/rules/{rule_id}", response_model=RuleOut, responses=ERROR_RESPONSES)
async def get_rule(rule_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    rule = await queries.get_rule(db, ctx, rule_id)
    return JSONResponse(
        await attach_package(db, ctx.tenant_id, "WorkRule", dump(RuleOut, rule)),
        headers={"ETag": format_etag(_RULE_ENTITY, rule.version)},
    )


@router.patch("/rules/{rule_id}", response_model=RuleOut, responses=ERROR_RESPONSES)
async def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, _RULE_ENTITY)
    provided = payload.model_dump(exclude_unset=True)
    for field in _NON_NULLABLE_PATCH_FIELDS:
        if field in provided and provided[field] is None:
            raise ValidationError("invalid_field", f"{field} cannot be null")

    def pick(field: str) -> Any:
        return provided.get(field, _UNSET)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        rule = await commands.update_rule(
            db,
            ctx,
            rule_id=rule_id,
            expected_version=expected_version,
            description=pick("description"),
            trigger=pick("trigger"),
            condition=pick("condition"),
            interpretation=pick("interpretation"),
            action=pick("action"),
            goal_id=pick("goal_id"),
            identity=pick("identity"),
        )
        return 200, await attach_package(db, ctx.tenant_id, "WorkRule", dump(RuleOut, rule))

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


async def _set_status(
    rule_id: uuid.UUID,
    status: str,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        rule = await commands.set_rule_status(db, ctx, rule_id=rule_id, status=status)
        return 200, await attach_package(db, ctx.tenant_id, "WorkRule", dump(RuleOut, rule))

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body=status, executor=executor
    )


@router.post(
    "/rules/{rule_id}:enable",
    response_model=RuleOut,
    responses=ERROR_RESPONSES,
    summary="Enable a rule: it acts from now on, with the caller's authority",
)
async def enable_rule(
    rule_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    return await _set_status(rule_id, RuleStatus.ENABLED, request, ctx, settings, session_factory)


@router.post(
    "/rules/{rule_id}:disable",
    response_model=RuleOut,
    responses=ERROR_RESPONSES,
    summary="Disable a rule: it stops evaluating new facts",
)
async def disable_rule(
    rule_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    return await _set_status(rule_id, RuleStatus.DISABLED, request, ctx, settings, session_factory)


@router.delete(
    "/rules/{rule_id}",
    status_code=204,
    responses=ERROR_RESPONSES,
    summary="Archive a rule; its history and the work it filed stay",
)
async def archive_rule(
    rule_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> Response:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.archive_rule(db, ctx, rule_id=rule_id)
        return 204, {}

    return as_no_content(
        await execute_write(
            request, ctx, settings, session_factory, canonical_body="", executor=executor
        )
    )


@router.get(
    "/rules/{rule_id}/evaluations",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="What the rule looked at and what it did, newest first",
)
async def list_rule_evaluations(
    rule_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_rule_evaluations(
        db, ctx, rule_id=rule_id, limit=limit, cursor=cursor, status=status
    )
    return JSONResponse(
        page_body([dump(RuleEvaluationOut, e) for e in page.items], page.next_cursor)
    )


@router.get(
    "/rule-evaluations/{evaluation_id}",
    response_model=RuleEvaluationOut,
    responses=ERROR_RESPONSES,
    summary="One evaluation by id: what origin.ref = rule_evaluation:<id> points to",
)
async def get_rule_evaluation(evaluation_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    evaluation = await queries.get_rule_evaluation(db, ctx, evaluation_id)
    return JSONResponse(dump(RuleEvaluationOut, evaluation))
