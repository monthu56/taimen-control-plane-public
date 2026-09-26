"""IAM identity binding: federated Principal -> local principal and permissions (IAM-7).

IAM confirms who arrived but grants nothing inside the Control Plane: the token
carries no domain permissions and must not. So between an external identity and
local authorization there has to be a record the Control Plane owns.

A binding is a read-only identity projection plus a local decision about rights.
`(issuer, iam_principal_id)` is unique, so one upstream identity never gets two
local Principals, and `iam_tenant_id` is kept alongside so that a token from
another tenant cannot work by a coincidence of subject.

The local revocation policy lives here too: `status` and `revoked_at` close
entry immediately, without waiting out an already issued access token.

Revision ID: f5b91c3e7a24
Revises: a7f2c4d19b60
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f5b91c3e7a24"
down_revision: str | None = "a7f2c4d19b60"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "iam_principal_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("iam_tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iam_principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="status"),
    )
    op.create_index(
        "uq_iam_bindings_identity",
        "iam_principal_bindings",
        ["issuer", "iam_principal_id"],
        unique=True,
    )
    op.create_index(
        "ix_iam_bindings_tenant_principal",
        "iam_principal_bindings",
        ["tenant_id", "principal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_iam_bindings_tenant_principal", table_name="iam_principal_bindings")
    op.drop_index("uq_iam_bindings_identity", table_name="iam_principal_bindings")
    op.drop_table("iam_principal_bindings")
