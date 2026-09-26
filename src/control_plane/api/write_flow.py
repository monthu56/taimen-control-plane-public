"""Shared execution path for mutating endpoints.

Every create/action POST (and PATCH) goes through here: with an
``Idempotency-Key`` header the command runs under the idempotency protocol;
without one it simply runs in its own transaction.
"""

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext
from control_plane.config import Settings
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.idempotency.service import (
    Executor,
    request_fingerprint,
    run_idempotent,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
_MAX_KEY_LENGTH = 200


async def execute_write(
    request: Request,
    ctx: AuthContext,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    canonical_body: str,
    executor: Executor,
    sensitive_fields: tuple[str, ...] = (),
) -> JSONResponse:
    idempotency_key = request.headers.get(IDEMPOTENCY_HEADER)

    if idempotency_key is None and ctx.purpose_ref is not None:
        # A decision from a channel arrives through retrying adapters (a
        # repeated button press, a redelivered webhook): without a key a retry
        # would be a second decision attempt instead of a replay (CP-ADR-0070).
        raise ValidationError(
            "idempotency_key_required",
            f"{IDEMPOTENCY_HEADER} is required for a purpose-bound credential",
        )

    if idempotency_key is None:
        async with transaction(session_factory) as session:
            status_code, body = await executor(session)
        return JSONResponse(status_code=status_code, content=body)

    idempotency_key = idempotency_key.strip()
    if not idempotency_key or len(idempotency_key) > _MAX_KEY_LENGTH:
        raise ValidationError(
            "invalid_idempotency_key",
            f"Idempotency-Key must be 1..{_MAX_KEY_LENGTH} characters",
        )

    status_code, body, replayed = await run_idempotent(
        session_factory,
        settings,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        key=idempotency_key,
        method=request.method,
        path=request.url.path,
        request_hash=request_fingerprint(request.method, request.url.path, canonical_body),
        executor=executor,
        sensitive_fields=sensitive_fields,
    )
    headers = {"Idempotency-Replayed": "true"} if replayed else None
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def as_no_content(response: JSONResponse) -> Response:
    """Convert an executor's 204 marker response into a true empty 204."""
    if response.status_code != 204:
        return response
    replayed = response.headers.get("idempotency-replayed")
    headers = {"Idempotency-Replayed": replayed} if replayed else None
    return Response(status_code=204, headers=headers)
