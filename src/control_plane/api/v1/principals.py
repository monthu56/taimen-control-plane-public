"""Principals, API keys and IAM identity bindings."""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import (
    AuthDep,
    DbDep,
    SessionFactoryDep,
    SettingsDep,
    get_iam_enforcement,
)
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ApiKeyCreatedOut,
    ApiKeyCreateRequest,
    ApiKeyOut,
    IamBindingOut,
    IamBindingUpsertRequest,
    PageOut,
    PrincipalCreateRequest,
    PrincipalDisableRequest,
    PrincipalEnabledOut,
    PrincipalEnableRequest,
    PrincipalOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import iam_bindings, principal_disable, principal_enable
from control_plane.application.commands import principals as commands
from control_plane.application.queries import lists as queries

router = APIRouter(tags=["principals"])


@router.post(
    "/principals",
    response_model=PrincipalOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def create_principal(
    payload: PrincipalCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        principal = await commands.create_principal(
            db,
            ctx,
            kind=payload.kind,
            display_name=payload.display_name,
            metadata=payload.metadata,
            status=payload.status,
        )
        return 201, dump(PrincipalOut, principal)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/principals", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_principals(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_principals(db, ctx, limit=limit, cursor=cursor, kind=kind)
    return JSONResponse(page_body([dump(PrincipalOut, p) for p in page.items], page.next_cursor))


@router.get("/principals/{principal_id}", response_model=PrincipalOut, responses=ERROR_RESPONSES)
async def get_principal(principal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    principal = await queries.get_principal(db, ctx, principal_id)
    return JSONResponse(dump(PrincipalOut, principal))


@router.post(
    "/principals/{principal_id}:disable",
    response_model=PrincipalOut,
    responses=ERROR_RESPONSES,
    summary="Disable a human or agent: revoke its bindings, close its sessions, free its claims",
)
async def disable_principal(
    principal_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: PrincipalDisableRequest | None = None,
) -> JSONResponse:
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await principal_disable.disable_principal(
            db, ctx, principal_id=principal_id, reason=payload.reason if payload else None
        )
        touched.extend(result.touched_identities)
        return 200, dump(PrincipalOut, result.principal)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
        executor=executor,
        # Takes the caller with the target principal, in id order.
        lock_caller_first=False,
    )
    for issuer, iam_principal_id in touched:
        forget_binding_cache(request, issuer, iam_principal_id)
    return response


@router.post(
    "/principals/{principal_id}:enable",
    response_model=PrincipalEnabledOut,
    responses=ERROR_RESPONSES,
    summary="Enable a disabled human or agent; IAM bindings revoked by :disable stay revoked",
)
async def enable_principal(
    principal_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: PrincipalEnableRequest | None = None,
) -> JSONResponse:
    touched: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await principal_enable.enable_principal(
            db, ctx, principal_id=principal_id, reason=payload.reason if payload else None
        )
        touched.extend(result.touched_identities)
        return 200, dump(PrincipalOut, result.principal, liveApiKeys=result.live_api_keys)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
        executor=executor,
        # Takes the caller with the target principal, in id order.
        lock_caller_first=False,
    )
    # Answers cached while it was disabled (``principal_not_active``, a revoked
    # binding) must not outlive the change: the next request reads the base.
    for issuer, iam_principal_id in touched:
        forget_binding_cache(request, issuer, iam_principal_id)
    return response


@router.post(
    "/principals/{principal_id}/api-keys",
    response_model=ApiKeyCreatedOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Issue an API key; the full key is returned only in this response",
)
async def create_api_key(
    principal_id: uuid.UUID,
    payload: ApiKeyCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        created = await commands.create_api_key(
            db,
            ctx,
            principal_id=principal_id,
            permissions=payload.permissions,
            expires_at=payload.expires_at,
        )
        return 201, dump(ApiKeyOut, created.api_key, key=created.generated.full_key)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
        # The full key is a one-time secret: never persisted in the
        # idempotency store, so a replay returns the record with key=null.
        sensitive_fields=("key",),
    )


@router.post(
    "/api-keys/{api_key_id}:revoke",
    response_model=ApiKeyOut,
    responses=ERROR_RESPONSES,
)
async def revoke_api_key(
    api_key_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        api_key = await commands.revoke_api_key(db, ctx, api_key_id=api_key_id)
        return 200, dump(ApiKeyOut, api_key)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )


# --- IAM identity bindings (ADR-0053) -----------------------------------------


def forget_binding_cache(request: Request, issuer: str, iam_principal_id: uuid.UUID) -> None:
    """Drop the enforcement cache for one identity once its binding changed.

    Done after the transaction committed, never inside it: a cache dropped for
    a write that then rolls back would just be reloaded with the old row, but
    a cache kept for a write that committed would let a revoked identity in
    for a whole TTL.
    """
    enforcement = get_iam_enforcement(request)
    if enforcement is not None:
        enforcement.bindings.invalidate(issuer, iam_principal_id)


@router.get(
    "/principals/{principal_id}/iam-bindings",
    responses=ERROR_RESPONSES,
    summary="Federated identities bound to a principal, revoked ones included",
)
async def list_iam_bindings(principal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    bindings = await queries.list_iam_bindings(db, ctx, principal_id)
    return JSONResponse({"items": [dump(IamBindingOut, b) for b in bindings]})


@router.post(
    "/principals/{principal_id}/iam-bindings",
    response_model=IamBindingOut,
    responses={**ERROR_RESPONSES, 201: {"model": IamBindingOut}},
    summary="Bind an IAM identity to a principal (upsert by issuer + IAM principal)",
)
async def upsert_iam_binding(
    principal_id: uuid.UUID,
    payload: IamBindingUpsertRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await iam_bindings.upsert_iam_binding(
            db,
            ctx,
            principal_id=principal_id,
            issuer=payload.issuer,
            iam_tenant_id=payload.iam_tenant_id,
            iam_principal_id=payload.iam_principal_id,
            permissions=payload.permissions,
            trusted_issuer=settings.iam_issuer,
        )
        return (201 if result.created else 200), dump(IamBindingOut, result.binding)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
    forget_binding_cache(request, payload.issuer, payload.iam_principal_id)
    return response


@router.post(
    "/iam-bindings/{binding_id}:revoke",
    response_model=IamBindingOut,
    responses=ERROR_RESPONSES,
    summary="Close entry for a federated identity without waiting for its token to expire",
)
async def revoke_iam_binding(
    binding_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    identity: list[tuple[str, uuid.UUID]] = []

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        binding = await iam_bindings.revoke_iam_binding(db, ctx, binding_id=binding_id)
        identity.append((binding.issuer, binding.iam_principal_id))
        return 200, dump(IamBindingOut, binding)

    response = await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )
    for issuer, iam_principal_id in identity:
        forget_binding_cache(request, issuer, iam_principal_id)
    return response
