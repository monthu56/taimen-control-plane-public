"""v0.6 -> integrated v0.7 -> v0.6 migration matrix.

The v0.7 merge head combines Effective Harness Manifest and Durable Active
Turn Control, which were developed as parallel additive revisions from v0.6.
Durable Child Run Handle extends the same line, so the roundtrip is always run
against the current head rather than a frozen intermediate revision: the
server code expects the schema of the head it ships with.
"""

from collections.abc import Iterator

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap, open_session

V06_HEAD = "72ef8bc31a06"
CURRENT_HEAD = "e8a4c2f6b1d9"
MANIFEST_TABLES = {"run_harness_manifests", "run_manifest_ephemerals"}
CHILD_TABLES = {"run_child_handles", "run_child_results"}

pytestmark = pytest.mark.usefixtures("clean_database")


@pytest.fixture
def v07_alembic_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def _run_with_manifest(
    client: httpx.AsyncClient, admin_key: str, *, agent: str = "agent-1"
) -> tuple[str, str]:
    _, agent_key = await create_agent_with_key(client, admin_key, name=agent)
    task = await create_task(client, admin_key)
    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()
    return agent_key, run["id"]


async def test_manifest_migration_roundtrip(
    client: httpx.AsyncClient, sync_engine: Engine, v07_alembic_config: Config
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent_key, run_id = await _run_with_manifest(client, admin_key)
    body = (
        await client.get(f"/api/v1/runs/{run_id}/harness-manifest", headers=auth(agent_key))
    ).json()
    assert body["manifest"]["version"] == 1

    alembic_command.downgrade(v07_alembic_config, V06_HEAD)
    tables = set(inspect(sync_engine).get_table_names())
    assert not (MANIFEST_TABLES & tables)
    assert not (CHILD_TABLES & tables)
    assert "run_control_messages" not in tables

    alembic_command.upgrade(v07_alembic_config, "head")
    with sync_engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        remaining = connection.execute(
            text("SELECT count(*) FROM run_harness_manifests")
        ).scalar_one()
    assert revision == CURRENT_HEAD
    assert remaining == 0

    agent_key, run_id = await _run_with_manifest(client, admin_key, agent="agent-2")
    again = await client.get(f"/api/v1/runs/{run_id}/harness-manifest", headers=auth(agent_key))
    assert again.status_code == 200, again.text
    assert again.json()["manifest"]["version"] == 1


async def test_immutability_trigger_survives_the_roundtrip(
    client: httpx.AsyncClient, sync_engine: Engine, v07_alembic_config: Config
) -> None:
    alembic_command.downgrade(v07_alembic_config, V06_HEAD)
    alembic_command.upgrade(v07_alembic_config, "head")
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _run_with_manifest(client, admin_key)

    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as connection:
        connection.execute(text("DELETE FROM run_harness_manifests"))


def test_active_turn_control_migration_roundtrip(
    sync_engine: Engine, v07_alembic_config: Config
) -> None:
    inspector = inspect(sync_engine)
    assert "run_control_messages" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("run_control_messages")} >= {
        "run_id",
        "seq",
        "operation",
        "status",
        "causal_position",
        "idempotency_key",
        "version",
        "resolved_at",
    }
    assert {index["name"] for index in inspector.get_indexes("run_control_messages")} >= {
        "ix_run_control_messages_run",
        "ix_run_control_messages_tenant_run",
        "ix_run_control_messages_accepted",
    }

    alembic_command.downgrade(v07_alembic_config, V06_HEAD)
    tables = set(inspect(sync_engine).get_table_names())
    assert "run_control_messages" not in tables
    assert not (MANIFEST_TABLES & tables)
    assert not (CHILD_TABLES & tables)

    alembic_command.upgrade(v07_alembic_config, "head")
    with sync_engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == CURRENT_HEAD
    tables = set(inspect(sync_engine).get_table_names())
    assert "run_control_messages" in tables
    assert tables >= MANIFEST_TABLES
    assert tables >= CHILD_TABLES
