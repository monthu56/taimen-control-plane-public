"""attention_feedback: a principal's verdict on an item of its attention list (CP-ADR-0071)

Revision ID: e8a4c2f6b1d9
Revises: d2f8b4a6e1c3
Create Date: 2026-09-26

Schema only. ``GET /me/attention`` computes its items on every read and stores
nothing; ``POST /me/attention/{itemKey}:feedback`` keeps one row per
``(tenant, principal, item_key)`` — a repeated verdict replaces the previous
one — with the rule, its version, the reason and the score the item had when
it was judged. ``verdict`` is ``useful`` or ``not_needed``.

Downgrade is lossy: the verdicts are dropped. Nothing else refers to them, and
the journal keeps an ``attention.feedback_recorded`` event per verdict.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8a4c2f6b1d9"
down_revision: str | None = "d2f8b4a6e1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attention_feedback",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("principal_id", sa.UUID(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("rule_key", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('useful', 'not_needed')", name=op.f("ck_attention_feedback_verdict")
        ),
        sa.CheckConstraint(
            "rule_version >= 1", name=op.f("ck_attention_feedback_rule_version_positive")
        ),
        sa.CheckConstraint(
            "score BETWEEN 0 AND 100", name=op.f("ck_attention_feedback_score_range")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_attention_feedback_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_attention_feedback_principal_id_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attention_feedback")),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "item_key", name="uq_attention_feedback_item"
        ),
    )
    op.create_index(
        "ix_attention_feedback_rule",
        "attention_feedback",
        ["tenant_id", "rule_key", "rule_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_attention_feedback_rule", table_name="attention_feedback")
    op.drop_table("attention_feedback")
