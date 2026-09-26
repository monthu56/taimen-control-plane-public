from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from tests.event_contract import journal_violations


@pytest.fixture(autouse=True)
def _clean(
    clean_database: None, sync_engine: Engine, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Every test in this package starts from an empty, migrated database —
    and leaves a journal that matches the event catalog (CP-ADR-0068), unless
    it wrote events past the core on purpose (``raw_journal``)."""
    yield
    if request.node.get_closest_marker("raw_journal"):
        return
    violations = journal_violations(sync_engine)
    assert not violations, "journal events do not match the event catalog:\n" + "\n".join(
        violations
    )
