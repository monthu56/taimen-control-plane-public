"""May I do X on Y (CP-ADR-0055, amendment of 2026-09-29).

A read: no Idempotency-Key, nothing is written. The answer comes from the
gates of the endpoints themselves (``application/queries/authz_check.py``).
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import AuthDep, DbDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    AuthzCheckOut,
    AuthzCheckRequest,
    AuthzCheckResultOut,
    AuthzDenialOut,
)
from control_plane.application.queries import authz_check

router = APIRouter(tags=["authz"])


@router.post(
    "/authz:check",
    response_model=AuthzCheckOut,
    responses=ERROR_RESPONSES,
    summary="Whether the caller may perform each action on its resource, as the endpoint decides",
)
async def check(payload: AuthzCheckRequest, ctx: AuthDep, db: DbDep) -> JSONResponse:
    results = await authz_check.check(
        db,
        ctx,
        [authz_check.CheckItem(c.action, c.resource_type, c.resource_id) for c in payload.checks],
    )
    body = AuthzCheckOut(
        results=[
            AuthzCheckResultOut(
                action=r.item.action,
                resource_type=r.item.resource_type,
                resource_id=r.item.resource_id,
                allowed=r.allowed,
                reason=AuthzDenialOut(**r.reason) if r.reason is not None else None,
            )
            for r in results
        ]
    )
    return JSONResponse(body.model_dump(mode="json", by_alias=True))
