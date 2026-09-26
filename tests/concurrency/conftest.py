import pytest


@pytest.fixture(autouse=True)
def _clean(clean_database: None) -> None:
    """Every test in this package starts from an empty, migrated database."""
