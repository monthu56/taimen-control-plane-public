"""v0.8: work item comments and their edit history (ADR-0050).

Two new tables and nothing touched:

1. ``task_comments`` — one reply in a work item's thread. The author is a
   Principal, the body is bounded by a CHECK, and a composite FK pins the
   comment and its task to the same tenant at the database level, exactly as
   ``task_relations`` does for its endpoints. The thread index carries
   ``(tenant_id, task_id, created_at, id)`` because the thread is read forward
   and the cursor compares that ascending pair.

2. ``task_comment_revisions`` — the superseded versions. Append-only, enforced
   by a trigger in the same shape as the one on ``events``: an edit writes the
   old text here first, so the record of what was said cannot be rewritten by
   whoever said it, whatever the application connects as.

Operational notes:

* purely additive — no existing table is altered and no row is rewritten, so
  the revision is safe to apply while the API is serving;
* ``edited_at_matches_version`` states the invariant the application maintains
  (``version = 1`` exactly when the comment was never edited), so "was this
  edited?" is answerable from one column and cannot drift;
* downgrade drops both tables. That is lossy in the ordinary way — the thread
  and its audit are gone — and nothing else depends on them, so the rollback is
  otherwise clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d3f1a45c72"
down_revision: str | None = "a1c7e94b2f60"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_REVISION_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION forbid_comment_revision_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'task_comment_revisions are append-only (% rejected)', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;
"""


def upgrade() -> None:
    op.create_table(
        "task_comments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("author_principal_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("artifact_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("edited_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 10000", name=op.f("ck_task_comments_body_length")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_task_comments_version_positive")),
        sa.CheckConstraint(
            "(version = 1) = (edited_at IS NULL)",
            name=op.f("ck_task_comments_edited_at_matches_version"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            name="fk_task_comments_task",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_task_comments_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["author_principal_id"],
            ["principals.id"],
            name=op.f("fk_task_comments_author_principal_id_principals"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_task_comments_run_id_runs")),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], name=op.f("fk_task_comments_artifact_id_artifacts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_comments")),
    )
    op.create_index(
        "ix_task_comments_thread", "task_comments", ["tenant_id", "task_id", "created_at", "id"]
    )
    op.create_index(
        "ix_task_comments_author", "task_comments", ["tenant_id", "author_principal_id"]
    )

    op.create_table(
        "task_comment_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("comment_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_principal_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("superseded_by", sa.UUID(), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_task_comment_revisions_version_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_task_comment_revisions_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["task_comments.id"],
            name=op.f("fk_task_comment_revisions_comment_id_task_comments"),
        ),
        sa.ForeignKeyConstraint(
            ["author_principal_id"],
            ["principals.id"],
            name=op.f("fk_task_comment_revisions_author_principal_id_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["principals.id"],
            name=op.f("fk_task_comment_revisions_superseded_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_comment_revisions")),
        sa.UniqueConstraint("comment_id", "version", name="uq_task_comment_revisions_version"),
    )
    op.create_index(
        "ix_task_comment_revisions_comment", "task_comment_revisions", ["comment_id", "version"]
    )
    op.create_index(
        "ix_task_comment_revisions_tenant_created",
        "task_comment_revisions",
        ["tenant_id", "created_at", "id"],
    )

    # The edit history is append-only for the same reason the event journal is:
    # a trail that its subject can rewrite is not a trail.
    op.execute(_REVISION_APPEND_ONLY_FN)
    op.execute(
        "CREATE TRIGGER task_comment_revisions_append_only "
        "BEFORE UPDATE OR DELETE ON task_comment_revisions "
        "FOR EACH ROW EXECUTE FUNCTION forbid_comment_revision_mutation()"
    )
    op.execute(
        "CREATE TRIGGER task_comment_revisions_append_only_truncate "
        "BEFORE TRUNCATE ON task_comment_revisions "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_comment_revision_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER task_comment_revisions_append_only_truncate ON task_comment_revisions")
    op.execute("DROP TRIGGER task_comment_revisions_append_only ON task_comment_revisions")
    op.drop_index("ix_task_comment_revisions_tenant_created", table_name="task_comment_revisions")
    op.drop_index("ix_task_comment_revisions_comment", table_name="task_comment_revisions")
    op.drop_table("task_comment_revisions")
    op.execute("DROP FUNCTION forbid_comment_revision_mutation()")
    op.drop_index("ix_task_comments_author", table_name="task_comments")
    op.drop_index("ix_task_comments_thread", table_name="task_comments")
    op.drop_table("task_comments")
