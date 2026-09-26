"""IAM binding: ``revoked`` as a terminal status (ADR-0053).

The binding table gets a management API and with it a revoke operation. Until
now the only closing state was ``disabled`` — a reversible operator switch,
written by hand in SQL. A revocation through the API is a different fact: the
identity was deliberately cut off, and the record says so instead of looking
like a paused one.

Nothing else changes: the same ``revoked_at`` stamp is written, the enforcement
path already treats any non-``active`` status as closed, and an upsert of the
same identity reopens the row by setting it back to ``active``.

Operational notes:

* only the CHECK constraint is widened; no row is rewritten, so the revision
  is safe to apply while the API is serving;
* downgrade folds ``revoked`` back into ``disabled`` before narrowing the
  constraint — the closed state survives, the reason for it does not.

Revision ID: c2d8e4f6a1b3
Revises: b8d3f1a45c72
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c2d8e4f6a1b3"
down_revision: str | None = "b8d3f1a45c72"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# The short name: the metadata naming convention expands it to
# ``ck_iam_principal_bindings_status``, the name the table was created with.
_CONSTRAINT = "status"
_TABLE = "iam_principal_bindings"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, "status IN ('active', 'disabled', 'revoked')")


def downgrade() -> None:
    op.execute(f"UPDATE {_TABLE} SET status = 'disabled' WHERE status = 'revoked'")
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, "status IN ('active', 'disabled')")
