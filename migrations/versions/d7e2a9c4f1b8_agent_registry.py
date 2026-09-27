"""agent registry: agents, immutable revisions, observed state (CP-ADR-0073)

Revision ID: d7e2a9c4f1b8
Revises: c3f8a2d6e1b7
Create Date: 2026-09-27

* ``agents`` — one row per ``(tenant, key)``: the desired state (``state``,
  ``replicas``) beside the number of the current revision, the principal the
  core derived for the agent and the IAM identity it is bound to (§6).
* ``agent_revisions`` — the published specs. A trigger rejects every UPDATE
  and DELETE: a run names the revision it went by, and that snapshot must not
  move under it (FR-003).
* ``agent_status`` — one row per agent, written by the placement service only.
* ``runs.agent_revision_id`` — the revision a run went by; NULL for executors
  that are not registered agents and for every run before this revision.

Downgrade is lossy: the registry and the revision of every run are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7e2a9c4f1b8"
down_revision: str | None = "c3f8a2d6e1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AGENT_REVISION_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_agent_revision_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'agent_revisions rows are immutable (% rejected)', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""

_TS = postgresql.TIMESTAMP(timezone=True)
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("replicas", sa.Integer(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("principal_id", sa.UUID(), nullable=True),
        sa.Column("iam_issuer", sa.Text(), nullable=True),
        sa.Column("iam_tenant_id", sa.UUID(), nullable=True),
        sa.Column("iam_principal_id", sa.UUID(), nullable=True),
        sa.Column("retired_at", _TS, nullable=True),
        sa.Column("retired_by", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.CheckConstraint("status IN ('active', 'retired')", name=op.f("ck_agents_status")),
        sa.CheckConstraint("state IN ('running', 'stopped')", name=op.f("ck_agents_state")),
        sa.CheckConstraint(
            "replicas >= 0 AND replicas <= 100", name=op.f("ck_agents_replicas_range")
        ),
        sa.CheckConstraint(
            "current_revision >= 1", name=op.f("ck_agents_current_revision_positive")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_agents_version_positive")),
        sa.CheckConstraint(
            "(principal_id IS NULL) = (iam_principal_id IS NULL)",
            name=op.f("ck_agents_identity_with_principal"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_agents_tenant_id_tenants"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_agents_workspace_id_workspaces"
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], name="fk_agents_principal_id_principals"
        ),
        sa.ForeignKeyConstraint(
            ["retired_by"], ["principals.id"], name="fk_agents_retired_by_principals"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_agents_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_agents_tenant_key"),
        sa.UniqueConstraint("principal_id", name="uq_agents_principal_id"),
    )
    op.create_index("ix_agents_tenant_created", "agents", ["tenant_id", "created_at", "id"])

    op.create_table(
        "agent_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("spec", _JSONB, nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_agent_revisions_revision_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_agent_revisions_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_agent_revisions_agent_id_agents"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_agent_revisions_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_revisions"),
        sa.UniqueConstraint("agent_id", "revision", name="uq_agent_revisions_agent_revision"),
    )
    op.execute(_AGENT_REVISION_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER agent_revisions_immutable BEFORE UPDATE OR DELETE ON agent_revisions "
        "FOR EACH ROW EXECUTE FUNCTION forbid_agent_revision_mutation()"
    )

    op.create_table(
        "agent_status",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("reason_message", sa.Text(), nullable=True),
        sa.Column("observed_revision", sa.Integer(), nullable=True),
        sa.Column("node", sa.Text(), nullable=True),
        sa.Column("instances_desired", sa.Integer(), nullable=False),
        sa.Column("instances_ready", sa.Integer(), nullable=False),
        sa.Column("observed_at", _TS, nullable=False),
        sa.Column("reported_by", sa.UUID(), nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.CheckConstraint(
            "phase IN ('pending', 'running', 'waiting_for_node', 'crash_looping', "
            "'node_unavailable', 'stopped')",
            name=op.f("ck_agent_status_phase"),
        ),
        sa.CheckConstraint(
            "instances_desired >= 0 AND instances_ready >= 0",
            name=op.f("ck_agent_status_instances_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_agent_status_agent_id_agents"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_agent_status_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["reported_by"], ["principals.id"], name="fk_agent_status_reported_by_principals"
        ),
        sa.PrimaryKeyConstraint("agent_id", name="pk_agent_status"),
    )

    op.add_column("runs", sa.Column("agent_revision_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_runs_agent_revision_id_agent_revisions",
        "runs",
        "agent_revisions",
        ["agent_revision_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_runs_agent_revision_id_agent_revisions", "runs", type_="foreignkey")
    op.drop_column("runs", "agent_revision_id")
    op.drop_table("agent_status")
    op.execute("DROP TRIGGER IF EXISTS agent_revisions_immutable ON agent_revisions")
    op.execute("DROP FUNCTION IF EXISTS forbid_agent_revision_mutation()")
    op.drop_table("agent_revisions")
    op.drop_index("ix_agents_tenant_created", table_name="agents")
    op.drop_table("agents")
