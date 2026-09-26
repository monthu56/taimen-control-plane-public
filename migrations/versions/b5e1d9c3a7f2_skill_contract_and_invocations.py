"""skill contract v1 and skill_invocations (CP-ADR-0056 §1-2)

Revision ID: b5e1d9c3a7f2
Revises: b3e7d1f9c2a4
Create Date: 2026-09-23

Three changes:

1. ``skills`` gets ``side_effects``, ``risk_level`` and ``contract``. All three
   are nullable: an existing row (the 21 http skills of BidOps among them) has
   no contract and stays a catalog entry that the core cannot invoke. A row
   that does carry a contract must declare both columns, and its ``protocol``
   must be the protocol of ``contract.implementation`` — the CHECKs below.

2. A published version is immutable, enforced by a trigger in the manner of
   ``forbid_task_type_mutation``: everything that makes up the contract is
   frozen, ``status`` may only move forward (active -> deprecated -> disabled,
   active -> disabled), DELETE is rejected. ``description`` stays editable: it
   is a catalog summary, not part of the contract (ADR-0056 amendment).
   ``row_version``/``updated_at`` change together with the allowed columns.

3. ``skill_invocations`` — the durable invocation object with lease/fencing,
   unique ``(skill_id, idempotency_key)``.

No data is rewritten. Downgrade drops the table, the trigger and the columns;
the contracts published in the meantime are lost with them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5e1d9c3a7f2"
down_revision: str | None = "b3e7d1f9c2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SKILL_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_skill_version_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'skills rows are immutable (DELETE rejected)';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.name IS DISTINCT FROM OLD.name
        OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.protocol IS DISTINCT FROM OLD.protocol
        OR NEW.config IS DISTINCT FROM OLD.config
        OR NEW.input_schema IS DISTINCT FROM OLD.input_schema
        OR NEW.output_schema IS DISTINCT FROM OLD.output_schema
        OR NEW.side_effects IS DISTINCT FROM OLD.side_effects
        OR NEW.risk_level IS DISTINCT FROM OLD.risk_level
        OR NEW.contract IS DISTINCT FROM OLD.contract
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'skill version content is immutable; publish a new version';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
        AND NOT (
            (OLD.status = 'active' AND NEW.status IN ('deprecated', 'disabled'))
            OR (OLD.status = 'deprecated' AND NEW.status = 'disabled')
        )
    THEN
        RAISE EXCEPTION 'skill status may only move active -> deprecated -> disabled';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # --- 1. contract columns ------------------------------------------------
    op.add_column("skills", sa.Column("side_effects", sa.Text(), nullable=True))
    op.add_column("skills", sa.Column("risk_level", sa.Text(), nullable=True))
    op.add_column(
        "skills",
        sa.Column("contract", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_skills_side_effects",
        "skills",
        "side_effects IS NULL OR side_effects IN ('none', 'external_read', 'external_write')",
    )
    op.create_check_constraint(
        "ck_skills_risk_level",
        "skills",
        "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high')",
    )
    op.create_check_constraint(
        "ck_skills_contract_declares_policy",
        "skills",
        "contract IS NULL OR (side_effects IS NOT NULL AND risk_level IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_skills_contract_protocol",
        "skills",
        "contract IS NULL OR ("
        "contract->'implementation'->>'protocol' IN ('http', 'local', 'mcp') "
        "AND protocol = contract->'implementation'->>'protocol')",
    )

    # --- 2. immutability ----------------------------------------------------
    op.execute(_SKILL_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER skills_immutable BEFORE UPDATE OR DELETE ON skills "
        "FOR EACH ROW EXECUTE FUNCTION forbid_skill_version_mutation()"
    )

    # --- 3. invocations -----------------------------------------------------
    op.create_table(
        "skill_invocations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("skill_id", sa.UUID(), nullable=False),
        sa.Column(
            "inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("requested_by_kind", sa.Text(), nullable=False),
        sa.Column("requested_by_ref", sa.Text(), nullable=False),
        sa.Column("authority_principal_id", sa.UUID(), nullable=False),
        sa.Column("authorization_basis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("executor_principal_id", sa.UUID(), nullable=True),
        sa.Column("executor_session_id", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("heartbeat_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("artifact_id", sa.UUID(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_skill_invocations_status"),
        ),
        sa.CheckConstraint(
            "requested_by_kind IN ('principal', 'rule', 'approval', 'verification', 'run')",
            name=op.f("ck_skill_invocations_requested_by_kind"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND attempt >= 0 AND attempt <= max_attempts",
            name=op.f("ck_skill_invocations_attempts"),
        ),
        sa.CheckConstraint(
            "status <> 'running' OR (lease_expires_at IS NOT NULL "
            "AND executor_principal_id IS NOT NULL)",
            name=op.f("ck_skill_invocations_running_has_lease"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_skill_invocations_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name="fk_skill_invocations_skill_id_skills"
        ),
        sa.ForeignKeyConstraint(
            ["authority_principal_id"],
            ["principals.id"],
            name="fk_skill_invocations_authority_principal_id_principals",
        ),
        sa.ForeignKeyConstraint(
            ["executor_principal_id"],
            ["principals.id"],
            name="fk_skill_invocations_executor_principal_id_principals",
        ),
        sa.ForeignKeyConstraint(
            ["executor_session_id"],
            ["sessions.id"],
            name="fk_skill_invocations_executor_session_id_sessions",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_skill_invocations_task_id_tasks"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name="fk_skill_invocations_run_id_runs"),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], name="fk_skill_invocations_artifact_id_artifacts"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill_invocations"),
        sa.UniqueConstraint(
            "skill_id", "idempotency_key", name="uq_skill_invocations_skill_idempotency"
        ),
    )
    op.create_index(
        "ix_skill_invocations_tenant_created",
        "skill_invocations",
        ["tenant_id", "created_at", "id"],
    )
    # The executor's queue: only live rows are indexed.
    op.create_index(
        "ix_skill_invocations_queue",
        "skill_invocations",
        ["tenant_id", "status", "available_at"],
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index("ix_skill_invocations_task", "skill_invocations", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_invocations_task", table_name="skill_invocations")
    op.drop_index("ix_skill_invocations_queue", table_name="skill_invocations")
    op.drop_index("ix_skill_invocations_tenant_created", table_name="skill_invocations")
    op.drop_table("skill_invocations")
    op.execute("DROP TRIGGER IF EXISTS skills_immutable ON skills")
    op.execute("DROP FUNCTION IF EXISTS forbid_skill_version_mutation()")
    op.drop_constraint("ck_skills_contract_protocol", "skills", type_="check")
    op.drop_constraint("ck_skills_contract_declares_policy", "skills", type_="check")
    op.drop_constraint("ck_skills_risk_level", "skills", type_="check")
    op.drop_constraint("ck_skills_side_effects", "skills", type_="check")
    op.drop_column("skills", "contract")
    op.drop_column("skills", "risk_level")
    op.drop_column("skills", "side_effects")
