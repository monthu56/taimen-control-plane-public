"""``agent:<key>`` in an assignment field (CP-ADR-0073, amendment A1).

An assignment field names a principal by id or an agent of the registry by
its stable key, so documents, packages and rules need not carry the ids of
one installation. The reference is resolved when the task is written, to the
principal the core derived for the agent; the task stores and returns that
id, never the reference.

Refused with ``422 unknown_agent`` (``details: {field, agent}``) when the
tenant has no agent with the key, the agent is retired, or it has no
principal to assign yet. No right beyond the write that carries the field is
needed: resolving the reference reveals only the id the task then shows.
"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.domain.enums import AgentStatus, PrincipalStatus
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.models import Agent, Principal

AGENT_REFERENCE_PREFIX = "agent:"
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def is_agent_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(AGENT_REFERENCE_PREFIX)


async def agent_principal(
    session: AsyncSession, tenant_id: uuid.UUID, reference: str, *, field: str
) -> uuid.UUID:
    """The principal of the agent ``reference`` names, or ``unknown_agent``."""
    key = reference.removeprefix(AGENT_REFERENCE_PREFIX)
    agent: Agent | None = None
    if _KEY_RE.match(key):
        agent = await session.scalar(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.key == key)
        )
    principal = (
        await session.get(Principal, agent.principal_id)
        if agent is not None and agent.status == AgentStatus.ACTIVE and agent.principal_id
        else None
    )
    if principal is None or principal.status == PrincipalStatus.DISABLED:
        raise ValidationError(
            "unknown_agent",
            f"{field}: {reference[:100]!r} names no active agent with an identity to assign",
            details={"field": field, "agent": key[:100]},
        )
    return principal.id


async def resolve_assignee(
    session: AsyncSession, tenant_id: uuid.UUID, value: uuid.UUID | str | None, *, field: str
) -> uuid.UUID | None:
    """An assignee as a principal id: an id as given, a reference resolved."""
    if isinstance(value, str):
        return await agent_principal(session, tenant_id, value, field=field)
    return value
