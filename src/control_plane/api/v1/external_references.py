"""Generic external reference endpoints.

The entity is named in the body and in the query string rather than in the
path, because the reference is a mapping onto *some* internal entity, not a
sub-resource of one particular kind (ADR-0047). The project-scoped routes in
``projects.py`` remain, and now run through this same command.
"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ExternalReferenceOut,
    ExternalReferenceRegisterRequest,
    PageOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import external_references as commands
from control_plane.application.queries import external_references as queries

router = APIRouter(tags=["external-references"])


@router.post(
    "/external-references",
    response_model=ExternalReferenceOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def register_external_reference(
    payload: ExternalReferenceRegisterRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        reference, created = await commands.register_external_reference(
            db,
            ctx,
            entity_type=payload.entity_type,
            entity_ref=payload.entity_id,
            external_system=payload.external_system,
            external_type=payload.external_type,
            external_id=payload.external_id,
            metadata=payload.metadata,
        )
        return (201 if created else 200), dump(ExternalReferenceOut, reference)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/external-references", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_external_references(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    entity_type: str | None = Query(default=None, alias="entityType"),
    entity_id: str | None = Query(default=None, alias="entityId"),
    external_system: str | None = Query(default=None, alias="externalSystem"),
    external_type: str | None = Query(default=None, alias="externalType"),
    external_id: str | None = Query(default=None, alias="externalId"),
) -> JSONResponse:
    queries.validate_lookup_arguments(
        entity_type=entity_type,
        entity_id=entity_id,
        external_system=external_system,
        external_type=external_type,
        external_id=external_id,
    )
    if entity_type is not None and entity_id is not None:
        page = await queries.list_entity_external_references(
            db,
            ctx,
            entity_type=entity_type,
            entity_ref=entity_id,
            limit=limit,
            cursor=cursor,
        )
    else:
        assert external_system is not None and external_id is not None  # validated above
        page = await queries.lookup_by_external_key(
            db,
            ctx,
            external_system=external_system,
            external_type=external_type,
            external_id=external_id,
            limit=limit,
            cursor=cursor,
        )
    return JSONResponse(
        page_body([dump(ExternalReferenceOut, r) for r in page.items], page.next_cursor)
    )
