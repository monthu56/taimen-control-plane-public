"""Helpers to compare the DB schema revision with the code's head revision."""

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def _find_alembic_ini() -> Path | None:
    candidate = Path.cwd() / "alembic.ini"
    if candidate.exists():
        return candidate
    # Fall back to the repository root relative to this file (src layout).
    candidate = Path(__file__).resolve().parents[4] / "alembic.ini"
    if candidate.exists():
        return candidate
    return None


@lru_cache
def get_head_revision() -> str | None:
    """The migration head according to the code, or None if scripts are absent."""
    ini_path = _find_alembic_ini()
    if ini_path is None:
        return None
    config = Config(str(ini_path))
    script_location = config.get_main_option("script_location", "migrations")
    if not Path(script_location).is_absolute():
        config.set_main_option("script_location", str(ini_path.parent / script_location))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()
