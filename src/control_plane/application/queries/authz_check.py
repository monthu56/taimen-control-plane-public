"""May I do X on Y: a batch of questions answered by the endpoints' own gates.

``POST /authz:check`` (CP-ADR-0055, amendment of 2026-09-29) exists so an
interface shows an action exactly when the core would accept it. Each action
is answered by the gate its command passes through — the same ``authorize``
calls on the same resource, the same organizational eligibility and
separation of duties — only without row locks and without the state checks
that follow authorization (a decided approval, a finished run): those are 409,
not 403, and the interface reads them from the resource itself. An approve
reads the context of its type's preconditions as the decider, so the check
reads it too — without evaluating them.

A gate may also refuse for what the resource is and will stay — a service
principal (422), a principal the agent registry owns or the caller itself
(409): the interface cannot read that off the principal, so the answer
carries the endpoint's own code. Only the pairs in ``RESOURCE_REFUSALS`` may
refuse so; a 409/422 from any other gate fails the whole batch. A repeat the
endpoint answers with ``200`` (enabling an active principal) is allowed.

Nothing is written: every gate only reads. A PDP that cannot answer fails the
whole batch with 503, as it fails the command.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands import (
    agents,
    approval_preconditions,
    approvals,
    principal_disable,
    principal_enable,
    runs,
    work_rules,
)
from control_plane.application.commands import process_instances as instances
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)

CHECK_BATCH_LIMIT = 100  # the same as AUTHZ_CHECK_MAX_ITEMS of the HTTP schema

Gate = Callable[[AsyncSession, AuthContext, str], Awaitable[object]]


def _uuid(resource_id: str) -> uuid.UUID:
    # Checked for the whole batch in ``check``: here it is always an id.
    return uuid.UUID(resource_id)


async def _reject(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    return await approvals.decision_gate(session, ctx, _uuid(resource_id), for_decision=False)


async def _approve(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    approval = await approvals.decision_gate(session, ctx, _uuid(resource_id), for_decision=False)
    # An approve also reads what the type's preconditions reference, as the
    # decider (403 without the right); whether they hold is the endpoint's 409.
    await approval_preconditions.read_preconditions(session, ctx, approval)
    return approval


async def _instance(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    return await instances.get_instance(
        session, ctx, _uuid(resource_id), Permission.PROCESSES_OPERATE
    )


async def _request_cancel(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    return await runs.request_cancel_gate(session, ctx, _uuid(resource_id))


async def _cancel_run(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    run = await runs.cancel_gate(session, ctx, _uuid(resource_id))
    runs.require_cancel_holder(ctx, run)
    return run


async def _rule(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    return await work_rules.rule_write_gate(session, ctx, _uuid(resource_id))


async def _agent(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    return await agents.state_gate(session, ctx, resource_id)


async def _enable_principal(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    return await principal_enable.enable_gate(session, ctx, _uuid(resource_id))


async def _disable_principal(session: AsyncSession, ctx: AuthContext, resource_id: str) -> object:
    return await principal_disable.disable_gate(session, ctx, _uuid(resource_id))


# (resourceType, action) -> the gate of the endpoint that performs it.
ACTIONS: dict[tuple[str, str], Gate] = {
    ("approval", "approve"): _approve,
    ("approval", "reject"): _reject,
    ("process_instance", "suspend"): _instance,
    ("process_instance", "resume"): _instance,
    ("process_instance", "cancel"): _instance,
    ("run", "request-cancel"): _request_cancel,
    ("run", "cancel"): _cancel_run,
    ("rule", "enable"): _rule,
    ("rule", "disable"): _rule,
    ("agent", "update-state"): _agent,
    ("principal", "enable"): _enable_principal,
    ("principal", "disable"): _disable_principal,
}

# Pairs whose gates also refuse for what the resource is (409/422), not only
# for the caller's rights: those codes go into the answer. Any other gate that
# raises 409/422 checks state after authorization, which ``authz:check`` must
# not answer (CP-ADR-0055): it fails the batch instead of reading as a denial.
RESOURCE_REFUSALS = frozenset({("principal", "enable"), ("principal", "disable")})

# Resource types addressed by UUID; an agent is addressed by its key.
UUID_TYPES = frozenset({"approval", "process_instance", "run", "rule", "principal"})

RESOURCE_TYPES = tuple(dict.fromkeys(t for t, _ in ACTIONS))
ACTION_NAMES = tuple(dict.fromkeys(a for _, a in ACTIONS))


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class CheckItem:
    action: str
    resource_type: str
    resource_id: str


@dataclass(frozen=True)
class CheckResult:
    item: CheckItem
    allowed: bool
    reason: dict[str, Any] | None


async def check(
    session: AsyncSession, ctx: AuthContext, items: list[CheckItem]
) -> list[CheckResult]:
    """One answer per item, in order. Any authenticated caller may ask about itself."""
    if not 1 <= len(items) <= CHECK_BATCH_LIMIT:
        raise ValidationError(
            "invalid_check",
            f"A check takes 1 to {CHECK_BATCH_LIMIT} items",
            details={"limit": CHECK_BATCH_LIMIT, "count": len(items)},
        )
    unknown = [
        {"action": i.action, "resourceType": i.resource_type}
        for i in items
        if (i.resource_type, i.action) not in ACTIONS
    ]
    if unknown:
        raise ValidationError(
            "unknown_action",
            "The action is not defined for this resource type",
            details={
                "unknown": unknown,
                "supported": [{"resourceType": t, "action": a} for t, a in ACTIONS],
            },
        )
    malformed = [
        i.resource_id
        for i in items
        if i.resource_type in UUID_TYPES and not _is_uuid(i.resource_id)
    ]
    if malformed:
        # The endpoint would refuse such a path as malformed, not as missing.
        raise ValidationError(
            "invalid_check",
            "The resource id is not a UUID",
            details={"resourceIds": malformed},
        )
    results = []
    for item in items:
        pair = (item.resource_type, item.action)
        try:
            await ACTIONS[pair](session, ctx, item.resource_id)
        except (ConflictError, ValidationError) as exc:
            if pair not in RESOURCE_REFUSALS:
                raise
            results.append(CheckResult(item, False, _reason(exc)))
        except (AuthorizationError, NotFoundError) as exc:
            # Only the gates raise here: the batch itself was validated above.
            results.append(CheckResult(item, False, _reason(exc)))
        else:
            results.append(CheckResult(item, True, None))
    return results


def _reason(exc: DomainError) -> dict[str, Any]:
    return {"code": exc.code, "message": exc.message, "details": exc.details}
