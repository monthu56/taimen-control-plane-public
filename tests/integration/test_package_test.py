"""``POST /packages:test`` at work (CP-ADR-0074 §10; process-packages P013).

The package goes as its files; the core checks every object, runs the tests
in its sandbox and reports coverage — and writes nothing: every table of the
database is the same, row for row, after the run as before it.
"""

import contextlib
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import false, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.commands import package_test
from control_plane.infrastructure.db.models import Role
from tests.helpers import auth, create_agent_with_key, create_workspace, do_bootstrap
from tests.integration.test_process_definitions import _catalog

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
# The sample's exit guard holds before anyone decided (null != ''): the review
# stage would close at once. The package under test waits for the decision.
PROCESS = (
    (FIXTURES / "sample.process.yaml")
    .read_text(encoding="utf-8")
    .replace("exit: data.decision != ''", "exit: has(data.decision)")
)
CALENDAR = (FIXTURES / "ru-2024.calendar.yaml").read_text(encoding="utf-8")
API_VERSION = yaml.safe_load(CALENDAR)["apiVersion"]  # the catalog format of the fixtures
MANIFEST = (
    f"apiVersion: {API_VERSION}\nkind: Package\nkey: sample\n"
    "spec: {version: 1.0.0, displayName: Sample}\n"
)


def scenario(summary: Any = "N-1: yes") -> dict[str, Any]:
    return {
        "process": "sample",
        "name": "a small amount is reviewed and reported",
        "given": {"clock": "2024-03-01T09:00:00Z", "principals": {"lead": ["alice"]}},
        "mocks": {
            "recall": [
                {"step": "history", "output": {"nodes": [{"kind": "case", "key": "sample:0"}]}}
            ],
            "skills": {"text.summarize@1": [{"output": {"summary": summary}}]},
        },
        "steps": [
            {
                "emit": {
                    "observation": "sample.opened",
                    "payload": {"number": "N-1", "amount": 10, "deadline": "2024-03-15T09:00:00Z"},
                }
            },
            {
                "expect": {
                    "stages": {"review": "open", "report": "not_started"},
                    "tasks": [
                        {"step": "decide", "assignee": "alice", "due": "2024-03-13T09:00:00Z"}
                    ],
                    "memory": {"recalled": ["history"]},
                    "data": {"level": 1},
                }
            },
            {"complete": {"step": "decide", "by": "alice", "output": {"decision": "yes"}}},
            {
                "expect": {
                    "status": "completed",
                    "outcome": "done",
                    "data": {"summary": "N-1: yes"},
                    "noSideEffects": True,
                }
            },
        ],
    }


def package(*extra: tuple[str, str], process: str = PROCESS, test: Any = None) -> dict[str, Any]:
    files = [
        ("package.yaml", MANIFEST),
        ("processes/sample.yaml", process),
        ("tests/review.test.yaml", yaml.safe_dump(test or scenario(), allow_unicode=True)),
        *extra,
    ]
    return {"files": [{"path": path, "content": content} for path, content in files]}


def snapshot(engine: Engine) -> dict[str, str]:
    """Every table of the database, row for row, as one digest per table."""
    with engine.connect() as conn:
        tables = conn.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).all()
        return {
            table: conn.scalar(
                text(
                    f"SELECT md5(coalesce(string_agg(t::text, chr(10) ORDER BY t::text), ''))"
                    f' FROM "{table}" t'
                )
            )
            for table in tables
        }


async def _setup(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    await _catalog(client, key)
    return key


async def test_a_package_is_tested_with_coverage_and_nothing_is_written(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _setup(client)
    before = snapshot(sync_engine)

    response = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(key)
    )

    assert response.status_code == 200, response.text
    assert snapshot(sync_engine) == before
    body = response.json()
    assert body["status"] == "passed", body["tests"][0]["failures"]
    assert body["checkOnly"] is False
    (test,) = body["tests"]
    assert (test["file"], test["process"], test["status"], test["failures"]) == (
        "tests/review.test.yaml",
        "sample",
        "passed",
        [],
    )
    (coverage,) = body["coverage"]
    assert (coverage["process"], coverage["version"]) == ("sample", 1)
    assert coverage["elements"]["missing"] == []
    assert coverage["decisionRows"] == {"covered": 1, "total": 2, "missing": ["level/1"]}
    assert "history:timeout" in coverage["transitions"]["missing"]
    # Memory is not configured here: the regulations are one warning, not a refusal.
    (warning,) = body["problems"]
    assert (warning["code"], warning["severity"], warning["file"], warning["line"]) == (
        "governed_by_unchecked",
        "warning",
        "processes/sample.yaml",
        7,  # the line of spec:
    )


async def test_check_only_runs_no_test(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    response = await client.post(
        "/api/v1/packages:test?checkOnly=true", json={"package": package()}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["checkOnly"], body["tests"], body["coverage"]) == (
        "passed",
        True,
        [],
        [],
    )


async def test_an_invalid_package_names_the_file_and_line_and_runs_no_test(
    client: httpx.AsyncClient,
) -> None:
    key = await _setup(client)
    broken = PROCESS.replace("taskType: review", "taskType: reveiw")
    response = await client.post(
        "/api/v1/packages:test", json={"package": package(process=broken)}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "invalid" and body["tests"] == [] and body["coverage"] == []
    (error,) = [p for p in body["problems"] if p["severity"] == "error"]
    assert (error["code"], error["file"]) == ("unknown_task_type", "processes/sample.yaml")
    assert PROCESS.splitlines()[error["line"] - 1].strip() == "taskType: review"


async def test_a_mock_off_the_skill_schema_fails_the_test(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(test=scenario(summary=42))},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    (failure,) = body["tests"][0]["failures"]
    assert failure["step"] == 2
    assert "text.summarize@1" in failure["message"] and failure["actual"] == {"summary": 42}


async def test_the_package_brings_what_its_process_names(client: httpx.AsyncClient) -> None:
    """Task type, skill, agent and calendar of the package are known before it is applied."""
    key: str = (await do_bootstrap(client))["apiKey"]["key"]

    def document(kind: str, name: str, spec: dict[str, Any]) -> str:
        return str(
            yaml.safe_dump({"apiVersion": API_VERSION, "kind": kind, "key": name, "spec": spec})
        )

    objects = (
        ("agents/sample-process.yaml", document("Agent", "sample-process", {"displayName": "S"})),
        (
            "task-types/review.yaml",
            document(
                "TaskType",
                "review",
                {"fieldSchema": {"type": "object", "properties": {"decision": {"type": "string"}}}},
            ),
        ),
        (
            "skills/summarize.yaml",
            document(
                "Skill",
                "text.summarize",
                {
                    "version": "1",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    "outputSchema": {
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                    },
                },
            ),
        ),
        ("calendars/ru.yaml", CALENDAR),
    )
    alone = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(key)
    )
    assert alone.json()["status"] == "invalid"
    response = await client.post(
        "/api/v1/packages:test", json={"package": package(*objects)}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "passed", response.json()


async def test_testing_a_package_needs_its_permission(client: httpx.AsyncClient) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    _, outsider = await create_agent_with_key(
        client, admin, name="outsider", permissions=["tasks.read"]
    )
    denied = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(outsider)
    )
    assert denied.status_code == 403, denied.text


async def test_a_read_through_raw_sql_is_no_side_effect(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """In a workspace the regulations read its ancestors through ``text(WITH RECURSIVE ...)``."""
    key = await _setup(client)
    workspace = await create_workspace(client, key, "tenders")
    process = PROCESS.replace("  version: 1\n", "  version: 1\n  workspaceId: ${workspace}\n", 1)
    before = snapshot(sync_engine)

    response = await client.post(
        "/api/v1/packages:test",
        json={"package": package(process=process), "workspaceId": workspace["id"]},
        headers=auth(key),
    )

    assert response.status_code == 200, response.text
    assert snapshot(sync_engine) == before
    body = response.json()
    assert body["status"] == "passed", body["tests"][0]["failures"]


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda: update(Role).where(false()).values(name="x"), id="orm"),
        pytest.param(lambda: text("DELETE FROM roles WHERE false"), id="text"),
    ],
)
async def test_an_attempt_to_write_is_a_side_effect(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, write: Any
) -> None:
    """A write the run attempts counts, though the READ ONLY transaction refuses it."""
    key = await _setup(client)
    role_slugs = package_test._role_slugs

    async def writing(session: AsyncSession, *args: Any) -> frozenset[str]:
        with contextlib.suppress(DBAPIError):
            async with session.begin_nested():
                await session.execute(write())
        return await role_slugs(session, *args)

    monkeypatch.setattr(package_test, "_role_slugs", writing)
    response = await client.post(
        "/api/v1/packages:test", json={"package": package()}, headers=auth(key)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    (failure,) = body["tests"][0]["failures"]
    assert "wrote outside the sandbox" in failure["message"] and failure["actual"] == 1
