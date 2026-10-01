"""The engine revision of a process version on Postgres (CP-ADR-0074, amendment 2026-09-29).

process-observability P010: a new publication writes ``engine_revision = 2``;
a version published before the amendment keeps ``1`` (the column's default)
and its instances run and replay under it, with zero discrepancies. A package
test runs a spec equal to such a version under ``1`` as well: the apply
returns that version, it does not publish the spec again.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from control_plane.application.commands import process_instances
from control_plane.application.commands.process_definitions import engine_revision_for
from control_plane.config import Settings
from control_plane.domain import process_engine as engine
from control_plane.domain.errors import ConflictError
from control_plane.domain.process_definition import normalized_spec
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import ProcessDefinition, ProcessInstance
from control_plane.worker.main import Worker
from tests.helpers import auth
from tests.integration.test_package_test import PROCESS as PACKAGE_PROCESS
from tests.integration.test_package_test import _setup as package_setup
from tests.integration.test_package_test import package
from tests.integration.test_process_instances import (
    _case,
    _instances,
    _observe,
    _publish,
    _setup,
)

LEGACY = "sample-legacy"


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


def _revisions(sync_engine: Engine) -> dict[str, int]:
    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT key, engine_revision FROM process_definitions"))
        return {key: revision for key, revision in rows}


async def test_a_version_published_before_the_revision_runs_and_replays_under_revision_1(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-case", _case(s["admin"]))
    # A version of before the amendment: written without engine_revision, as
    # every row the migration of the feature found (the column's default).
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO process_definitions (id, tenant_id, workspace_id, key, version,"
                " display_name, definition_hash, identity_agent, expression_profile, spec,"
                " governed_by, warnings, created_by, created_at)"
                " SELECT gen_random_uuid(), tenant_id, workspace_id, :legacy, version,"
                " display_name, definition_hash, identity_agent, expression_profile, spec,"
                " governed_by, warnings, created_by, created_at"
                " FROM process_definitions WHERE key = 'sample-case'"
            ),
            {"legacy": LEGACY},
        )
    assert _revisions(sync_engine) == {"sample-case": 2, LEGACY: 1}

    await _observe(client, key, "sample.opened", number="S-1", deadline="2030-01-01T09:00:00Z")
    await worker.run_once()
    [new] = await _instances(client, key, definitionKey="sample-case")
    [old] = await _instances(client, key, definitionKey=LEGACY)
    assert old["status"] == new["status"] == "running"
    assert old["data"] == new["data"]

    for instance, revision in ((old, 1), (new, 2)):
        async with transaction(worker.session_factory) as session:
            row = await session.get(ProcessInstance, instance["id"])
            assert row is not None
            version = await session.get(ProcessDefinition, row.definition_id)
            assert version is not None
            definition = await process_instances.definition_of(session, version)
            assert definition.engine_revision == revision
            result = await process_instances.replay_instance(session, row)
        assert [d.out() for d in result.discrepancies] == []
        assert result.steps == 1


def _downgrade(sync_engine: Engine) -> None:
    """Every version as if published before the amendment: past the immutability trigger."""
    with sync_engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE process_definitions SET engine_revision = 1"))


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """The key and revision of every definition the engine builds."""
    built: list[tuple[str, int]] = []
    build = engine.Definition.build

    def spy(*args: Any, **kwargs: Any) -> engine.Definition:
        definition = build(*args, **kwargs)
        built.append((definition.key, definition.engine_revision))
        return definition

    monkeypatch.setattr(engine.Definition, "build", staticmethod(spy))
    return built


async def test_a_package_test_runs_a_published_spec_under_the_revision_of_its_version(
    client: httpx.AsyncClient, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The apply of a spec equal to a published version returns that version:
    # the test must run it under the revision the live instances run under.
    key = await package_setup(client)
    plan = await client.post(
        "/api/v1/packages:plan", json={"package": package()}, headers=auth(key)
    )
    assert plan.status_code == 200, plan.text
    applied = await client.post(
        "/api/v1/packages:apply",
        json={"package": package(), "planHash": plan.json()["planHash"]},
        headers=auth(key),
    )
    assert applied.status_code == 200, applied.text
    assert _revisions(sync_engine) == {"sample": engine.ENGINE_REVISION}
    _downgrade(sync_engine)  # published before the amendment
    built = _spy(monkeypatch)

    async def check(body: dict[str, Any]) -> None:
        response = await client.post(
            "/api/v1/packages:test?checkOnly=true", json={"package": body}, headers=auth(key)
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "passed", response.text

    await check(package())
    assert built == [("sample", 1)]

    # A new version is published anew: under the latest revision.
    built.clear()
    newer = PACKAGE_PROCESS.replace("  version: 1\n", "  version: 2\n", 1)
    assert newer != PACKAGE_PROCESS
    await check(package(process=newer))
    assert built == [("sample", engine.ENGINE_REVISION)]


async def test_the_revision_of_a_spec_is_that_of_its_published_version_or_the_latest(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    spec = _case(s["admin"])
    await _publish(client, s["key"], "sample-case", spec)
    _downgrade(sync_engine)
    changed = {**spec, "displayName": "Sample case, changed"}
    newer = {**spec, "version": 2}
    async with transaction(worker.session_factory) as session:
        [tenant_id] = (await session.scalars(select(ProcessDefinition.tenant_id))).all()

        async def revision(key: str, body: dict[str, Any]) -> int:
            return await engine_revision_for(session, tenant_id, key, normalized_spec(body))

        assert await revision("sample-case", spec) == 1
        assert await revision("sample-case", changed) == engine.ENGINE_REVISION
        assert await revision("sample-case", newer) == engine.ENGINE_REVISION
        assert await revision("sample-other", spec) == engine.ENGINE_REVISION


async def test_a_version_of_an_unknown_revision_is_unusable_not_an_error(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    # Published by a newer release, the code rolled back since.
    s = await _setup(client)
    await _publish(client, s["key"], "sample-case", _case(s["admin"]))
    with sync_engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE process_definitions SET engine_revision = 99"))
    async with transaction(worker.session_factory) as session:
        [version] = (await session.scalars(select(ProcessDefinition))).all()
        with pytest.raises(ConflictError) as caught:
            await process_instances.definition_of(session, version)
    assert caught.value.code == "process_definition_unusable"
    assert caught.value.details["engineRevision"] == 99
