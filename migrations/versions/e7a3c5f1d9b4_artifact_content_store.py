"""artifacts: content in the store and the uploads that feed it (CP-ADR-0072)

Revision ID: e7a3c5f1d9b4
Revises: e8a4c2f6b1d9
Create Date: 2026-09-26

``artifacts`` gains ``content_state`` (none | stored | purged), ``size_bytes``,
``media_type``, ``sha256`` and ``type_version``. Existing rows are references
or small JSON: ``content_state = 'none'`` and the rest NULL, which is what the
server default gives them. ``artifact_contents`` records one row per upload
(``contentRef``) until an artifact references it or it expires.

Downgrade drops the table and the columns; artifacts that had content keep
their record and lose the pointer to the bytes (the objects stay in the store).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7a3c5f1d9b4"
down_revision: str | None = "e8a4c2f6b1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("content_state", sa.Text(), server_default=sa.text("'none'"), nullable=False),
    )
    op.add_column("artifacts", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("artifacts", sa.Column("media_type", sa.Text(), nullable=True))
    op.add_column("artifacts", sa.Column("sha256", sa.Text(), nullable=True))
    op.add_column("artifacts", sa.Column("type_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_artifacts_content_state_valid"),
        "artifacts",
        "content_state IN ('none', 'stored', 'purged')",
    )
    op.create_check_constraint(
        op.f("ck_artifacts_content_fields_consistent"),
        "artifacts",
        "(content_state = 'none') = (sha256 IS NULL)"
        " AND (sha256 IS NULL) = (size_bytes IS NULL)"
        " AND (sha256 IS NULL) = (media_type IS NULL)",
    )
    op.create_index(
        "ix_artifacts_tenant_sha256",
        "artifacts",
        ["tenant_id", "sha256"],
        unique=False,
        postgresql_where=sa.text("sha256 IS NOT NULL"),
    )

    op.create_table(
        "artifact_contents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("uploaded_by_principal_id", sa.UUID(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("referenced_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_artifact_contents_size_non_negative")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_artifact_contents_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_principal_id"],
            ["principals.id"],
            name=op.f("fk_artifact_contents_uploaded_by_principal_id_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_contents")),
    )
    op.create_index(
        "ix_artifact_contents_tenant_sha256",
        "artifact_contents",
        ["tenant_id", "sha256"],
        unique=False,
    )
    op.create_index(
        "ix_artifact_contents_expires", "artifact_contents", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_contents_expires", table_name="artifact_contents")
    op.drop_index("ix_artifact_contents_tenant_sha256", table_name="artifact_contents")
    op.drop_table("artifact_contents")
    op.drop_index("ix_artifacts_tenant_sha256", table_name="artifacts")
    op.drop_constraint(op.f("ck_artifacts_content_fields_consistent"), "artifacts", type_="check")
    op.drop_constraint(op.f("ck_artifacts_content_state_valid"), "artifacts", type_="check")
    op.drop_column("artifacts", "type_version")
    op.drop_column("artifacts", "sha256")
    op.drop_column("artifacts", "media_type")
    op.drop_column("artifacts", "size_bytes")
    op.drop_column("artifacts", "content_state")
