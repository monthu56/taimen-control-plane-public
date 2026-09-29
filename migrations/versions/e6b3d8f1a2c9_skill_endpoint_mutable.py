"""a published skill version may move its implementation endpoint (ADR-0056, 2026-09-29)

Revision ID: e6b3d8f1a2c9
Revises: d7f2a9c4e1b8
Create Date: 2026-09-29

``forbid_skill_version_mutation`` keeps freezing every field of a version,
with one exception: ``contract`` may differ from the old one only in
``implementation.endpoint`` — the address at which this installation reaches
the implementation, not what the skill promises. The contract itself cannot
appear or disappear, and the new endpoint must still be a string.

Downgrade restores the fully frozen contract; rows already moved keep their
new endpoint.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6b3d8f1a2c9"
down_revision: str | None = "d7f2a9c4e1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _function(contract_check: str) -> str:
    return f"""
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
        OR {contract_check}
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


_FROZEN_CONTRACT = "NEW.contract IS DISTINCT FROM OLD.contract"

_ENDPOINT_ONLY = """(NEW.contract IS DISTINCT FROM OLD.contract AND NOT (
            OLD.contract IS NOT NULL
            AND NEW.contract IS NOT NULL
            AND (NEW.contract - 'implementation') = (OLD.contract - 'implementation')
            AND (NEW.contract -> 'implementation') - 'endpoint'
                = (OLD.contract -> 'implementation') - 'endpoint'
            AND jsonb_typeof(NEW.contract -> 'implementation' -> 'endpoint') = 'string'
        ))"""


def upgrade() -> None:
    op.execute(_function(_ENDPOINT_ONLY))


def downgrade() -> None:
    op.execute(_function(_FROZEN_CONTRACT))
