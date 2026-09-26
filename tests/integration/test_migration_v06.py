"""v0.5 -> v0.6 -> v0.5 session migration matrix."""

from collections.abc import Iterator

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from tests.helpers import do_bootstrap, open_session

V05_HEAD = "1adf50721f1e"
V06_HEAD = "72ef8bc31a06"


@pytest.fixture
def v06_alembic_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_legacy_session_backfill_and_downgrade(
    client: httpx.AsyncClient, sync_engine: Engine, v06_alembic_config: Config
) -> None:
    boot = await do_bootstrap(client)
    session = await open_session(client, boot["apiKey"]["key"])
    assert session["controlLevel"] == "human_operated"

    alembic_command.downgrade(v06_alembic_config, V05_HEAD)
    assert "control_level" not in {
        column["name"] for column in inspect(sync_engine).get_columns("sessions")
    }

    alembic_command.upgrade(v06_alembic_config, V06_HEAD)
    with sync_engine.connect() as connection:
        control_level = connection.execute(
            text("SELECT control_level FROM sessions WHERE id = :id"), {"id": session["id"]}
        ).scalar_one()
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert control_level == "connected"
    assert revision == V06_HEAD
