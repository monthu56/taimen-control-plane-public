"""Merge the parallel v0.7 agent-runtime migration heads.

Revision ID: d4e6f8a1b2c3
Revises: 9c41ee0d7b52, c91f3c7ad8e2
"""

from collections.abc import Sequence

revision: str = "d4e6f8a1b2c3"
down_revision: tuple[str, str] = ("9c41ee0d7b52", "c91f3c7ad8e2")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent revisions are additive; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
