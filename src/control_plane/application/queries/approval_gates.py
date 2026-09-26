"""Pending gate approvals on a task (ADR-0018).

A read of its own so that the commands which consult a gate (claim, skill
invocation, discovery) do not import the approvals command module, which in
turn imports the outcome executor (CP-ADR-0061).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.domain.enums import ApprovalStatus
from control_plane.infrastructure.db.models import Approval


async def pending_gate_approvals(
    session: AsyncSession, tenant_id: uuid.UUID, task_id: uuid.UUID
) -> list[dict[str, str]]:
    """Pending gate approvals holding a task (empty list = no gate)."""
    rows = (
        await session.execute(
            select(Approval.id, Approval.requested_by_principal_id).where(
                Approval.tenant_id == tenant_id,
                Approval.task_id == task_id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    ).all()
    return [{"approvalId": str(row[0]), "requestedBy": str(row[1])} for row in rows]
