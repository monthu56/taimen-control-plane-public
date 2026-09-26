"""Durable Active Turn Control messages.

Revision ID: c91f3c7ad8e2
Revises: 72ef8bc31a06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c91f3c7ad8e2"
down_revision: str | None = "72ef8bc31a06"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_control_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'accepted'")),
        sa.Column("causal_position", sa.Text(), nullable=False),
        sa.Column("directive", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("safe_boundary", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("requested_by_principal_id", sa.UUID(), nullable=False),
        sa.Column("acknowledged_by_principal_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("causation_id", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "operation IN ('queue', 'steer', 'redirect', 'request_cancel', 'force_cancel')",
            name="ck_run_control_messages_operation",
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'applied', 'rejected', 'superseded')",
            name="ck_run_control_messages_status",
        ),
        sa.CheckConstraint("seq >= 1", name="ck_run_control_messages_seq_positive"),
        sa.CheckConstraint("version >= 1", name="ck_run_control_messages_version_positive"),
        sa.CheckConstraint(
            "(status = 'accepted' AND resolved_at IS NULL) OR "
            "(status <> 'accepted' AND resolved_at IS NOT NULL)",
            name="ck_run_control_messages_resolution_matches_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["requested_by_principal_id"], ["principals.id"]),
        sa.ForeignKeyConstraint(["acknowledged_by_principal_id"], ["principals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_control_messages_run_seq"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_run_control_messages_run_idempotency"
        ),
    )
    op.create_index("ix_run_control_messages_run", "run_control_messages", ["run_id", "seq"])
    op.create_index(
        "ix_run_control_messages_tenant_run",
        "run_control_messages",
        ["tenant_id", "run_id", "seq"],
    )
    op.create_index(
        "ix_run_control_messages_accepted",
        "run_control_messages",
        ["run_id", "seq"],
        postgresql_where=sa.text("status = 'accepted'"),
    )


def downgrade() -> None:
    op.drop_index("ix_run_control_messages_accepted", table_name="run_control_messages")
    op.drop_index("ix_run_control_messages_tenant_run", table_name="run_control_messages")
    op.drop_index("ix_run_control_messages_run", table_name="run_control_messages")
    op.drop_table("run_control_messages")
