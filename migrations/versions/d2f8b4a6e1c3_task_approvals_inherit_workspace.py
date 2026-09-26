"""approvals: a pending approval of a task lives in the task's workspace (CP-ADR-0068)

Revision ID: d2f8b4a6e1c3
Revises: c5e1a7d3f9b2
Create Date: 2026-09-25

Data only. Until this revision ``request_approval`` stored only an explicit
``workspaceId``: an approval about a task in a workspace was left without one,
so only tenant-wide holders of its role could decide it, while the
``approval.requested`` event and the role-holder listing named the task's
workspace. A pending approval of a task with ``workspace_id IS NULL`` gets the
task's workspace. Decided and cancelled approvals are history and stay as
they were; so do approvals without a task.

Downgrade is a no-op: the backfilled rows are indistinguishable from approvals
created with the task's workspace, and both stay valid under the old code.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d2f8b4a6e1c3"
down_revision: str | None = "c5e1a7d3f9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE approvals a
        SET workspace_id = t.workspace_id
        FROM tasks t
        WHERE a.task_id = t.id
          AND a.tenant_id = t.tenant_id
          AND a.status = 'pending'
          AND a.workspace_id IS NULL
          AND t.workspace_id IS NOT NULL
        """
    )


def downgrade() -> None:
    pass
