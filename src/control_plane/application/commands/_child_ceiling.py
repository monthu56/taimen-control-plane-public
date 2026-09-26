"""Enforcement of the child ceiling recorded on a handle (HRS-7).

Kept in its own module for the same reason ``_claim_release`` is: the run,
artifact, execution and manifest commands all need it, and none of them should
have to import each other to get it.

The rule is one line of algebra — the effective permissions of work done under
a child run are ``permissions(api key) ∩ granted(handle)`` — but the reason it
holds is structural: the ceiling was written at launch from the *parent's*
ceiling, so a child holding a stronger API key still cannot exceed its parent.

Skills are NOT narrowed here: that dimension travels inside the effective tool
policy (HRS-3), so search, describe, the manifest and the invocation gate all
narrow through one decision instead of four.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.domain.child_handle import Grant, grant_covers, grant_from_stored
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError
from control_plane.infrastructure.db.models import Run, RunChildHandle


async def ceiling_of_run(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[RunChildHandle, Grant] | None:
    """The handle and ceiling of a run, or ``None`` for a root run."""
    handle: RunChildHandle | None = await session.scalar(
        select(RunChildHandle).where(
            RunChildHandle.tenant_id == tenant_id,
            RunChildHandle.child_run_id == run_id,
        )
    )
    if handle is None:
        return None
    return handle, grant_from_stored(handle.granted)


async def _bounded_ceiling(
    session: AsyncSession, ctx: AuthContext, run: Run
) -> tuple[RunChildHandle, Grant] | None:
    """The ceiling that applies to *this actor* acting on ``run``.

    The grant bounds what the child itself may do. An operator acting on the
    child from outside — cancelling it, failing it, steering it — is bounded by
    their own permissions instead: that is oversight, not escalation, and it is
    not a way for the child to widen its own ceiling, since it cannot become
    another principal.
    """
    if run.principal_id != ctx.principal_id:
        return None
    return await ceiling_of_run(session, ctx.tenant_id, run.id)


async def enforce_run_ceiling(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run: Run,
    permission: Permission,
) -> None:
    """Reject an authoritative write by a child run that exceeds its grant.

    A root run has no handle and is unaffected — this narrows, it never widens.
    """
    bound = await _bounded_ceiling(session, ctx, run)
    if bound is None:
        return
    handle, granted = bound
    if not grant_covers(granted, permission):
        raise AuthorizationError(
            "This run is bounded by a child handle that does not grant this permission",
            code="child_grant_exceeded",
            details={
                "childHandleId": str(handle.id),
                "runId": str(run.id),
                "required": permission.value,
                "granted": list(granted.permissions),
            },
        )
