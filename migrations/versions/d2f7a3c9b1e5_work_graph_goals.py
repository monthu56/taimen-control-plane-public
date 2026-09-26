"""M1.1 work graph: goals, and goal/origin/acceptance/evidence on tasks (CP-ADR-0062)

Revision ID: d2f7a3c9b1e5
Revises: e3a9c5d7f1b4
Create Date: 2026-09-23

Schema:

* ``goals`` — a desired state: title, prose, criteria (the acceptance-check
  shape), owner, ``active|achieved|abandoned`` with ``closed_at`` pinned to the
  status by a CHECK, an immutable ``created_from`` (the origin shape) and an
  optional parent goal. Tenant-consistent composite FK for the parent.
* ``tasks.goal_id`` — composite FK ``(tenant_id, goal_id)`` → ``goals``, so a
  task can never serve another tenant's goal; a partial index serves
  ``GET /goals/{id}/work``.
* ``tasks.origin`` / ``tasks.acceptance`` / ``tasks.evidence`` — JSONB with
  CHECKs on the top-level type (an object with ``kind`` / arrays). The
  document shapes themselves are validated by the application
  (``domain/work_graph.py``).

Data: every existing task gets ``origin = {"kind": "human", "evidence": []}``
— the only way a task came to be before this revision (tasks spawned by an
approval outcome are indistinguishable at this point and stay ``human``;
their ``external_references`` row still records the approval). The value is
the column's server default, so PostgreSQL fills existing rows as part of
``ADD COLUMN`` without rewriting the table, and a writer that predates this
revision (a rolling deploy) keeps producing a valid row with the same
meaning. ``acceptance`` and ``evidence`` start empty.

Downgrade is lossy: goals, the links to them and every origin/acceptance/
evidence document are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2f7a3c9b1e5"
down_revision: str | None = "e3a9c5d7f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HUMAN_ORIGIN = """'{"kind": "human", "evidence": []}'::jsonb"""


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("desired_state", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("owner_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "created_from",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(_HUMAN_ORIGIN),
        ),
        sa.Column("parent_goal_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'achieved', 'abandoned')", name=op.f("ck_goals_status")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 500", name=op.f("ck_goals_title_length")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_goals_version_positive")),
        sa.CheckConstraint(
            "(status = 'active') = (closed_at IS NULL)", name=op.f("ck_goals_closed_at_matches")
        ),
        sa.CheckConstraint(
            "parent_goal_id IS DISTINCT FROM id", name=op.f("ck_goals_not_own_parent")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(criteria) = 'array'", name=op.f("ck_goals_criteria_is_array")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(created_from) = 'object' AND created_from ? 'kind'",
            name=op.f("ck_goals_created_from_has_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_goals_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_goals_workspace_id_workspaces")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["principals.id"], name=op.f("fk_goals_owner_id_principals")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name=op.f("fk_goals_created_by_principals")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goals")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_goals_tenant_id_id"),
    )
    # Created after the table: the target of a self-referencing composite FK
    # is the unique constraint above.
    op.create_foreign_key(
        "fk_goals_parent",
        "goals",
        "goals",
        ["tenant_id", "parent_goal_id"],
        ["tenant_id", "id"],
    )
    op.create_index("ix_goals_tenant_created", "goals", ["tenant_id", "created_at", "id"])
    op.create_index("ix_goals_tenant_status", "goals", ["tenant_id", "status"])
    op.create_index("ix_goals_parent", "goals", ["tenant_id", "parent_goal_id"])
    op.create_index("ix_goals_workspace", "goals", ["workspace_id"])

    op.add_column("tasks", sa.Column("goal_id", sa.UUID(), nullable=True))
    # Backfill by server default: every existing row becomes human-originated.
    op.add_column(
        "tasks",
        sa.Column(
            "origin",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(_HUMAN_ORIGIN),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "acceptance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_tasks_goal", "tasks", "goals", ["tenant_id", "goal_id"], ["tenant_id", "id"]
    )
    op.create_check_constraint(
        op.f("ck_tasks_origin_has_kind"),
        "tasks",
        "jsonb_typeof(origin) = 'object' AND origin ? 'kind'",
    )
    op.create_check_constraint(
        op.f("ck_tasks_acceptance_is_array"), "tasks", "jsonb_typeof(acceptance) = 'array'"
    )
    op.create_check_constraint(
        op.f("ck_tasks_evidence_is_array"), "tasks", "jsonb_typeof(evidence) = 'array'"
    )
    op.create_index(
        "ix_tasks_tenant_goal",
        "tasks",
        ["tenant_id", "goal_id", "created_at", "id"],
        postgresql_where=sa.text("goal_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_tenant_goal", table_name="tasks")
    op.drop_constraint(op.f("ck_tasks_evidence_is_array"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_acceptance_is_array"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_origin_has_kind"), "tasks", type_="check")
    op.drop_constraint("fk_tasks_goal", "tasks", type_="foreignkey")
    op.drop_column("tasks", "evidence")
    op.drop_column("tasks", "acceptance")
    op.drop_column("tasks", "origin")
    op.drop_column("tasks", "goal_id")

    op.drop_index("ix_goals_workspace", table_name="goals")
    op.drop_index("ix_goals_parent", table_name="goals")
    op.drop_index("ix_goals_tenant_status", table_name="goals")
    op.drop_index("ix_goals_tenant_created", table_name="goals")
    op.drop_constraint("fk_goals_parent", "goals", type_="foreignkey")
    op.drop_table("goals")
