"""Which package installed a catalog object: ``package`` and ``?package=`` (TASK-000904).

CP-ADR-0074 §11, amendment of 2026-09-29. What is pinned here:

- ``POST /packages:apply`` links the calendars and processes of the package;
  an apply of the next version of the package moves the link, even for an
  object it leaves unchanged;
- the installer links what it applied through the other routes with
  ``POST /packages:record``; an object created by hand is ``package: null``;
  a version published by hand later stays in the package of its key;
- ``POST /agents`` with ``package`` links the agent;
- ``?package=<key>`` keeps the page to the package's objects;
- the migration goes down and up, and links the agents whose revisions name
  a package.
"""

import copy
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_capability,
    create_role,
    create_workspace,
    do_bootstrap,
    register_skill,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
CALENDAR_TEXT = (FIXTURES / "ru-2024.calendar.yaml").read_text(encoding="utf-8")
CALENDAR: dict[str, Any] = yaml.safe_load(CALENDAR_TEXT)
BEFORE_LINKS = "a8e3c1f5b920"
PACKAGE = "links"


def _package(version: str) -> dict[str, Any]:
    manifest = {
        "apiVersion": CALENDAR["apiVersion"],
        "kind": "Package",
        "key": PACKAGE,
        "spec": {"version": version, "displayName": "Links"},
    }
    files = [("package.yaml", yaml.safe_dump(manifest)), ("calendars/ru.yaml", CALENDAR_TEXT)]
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def _apply(client: httpx.AsyncClient, key: str, version: str) -> str:
    package = _package(version)
    plan = await client.post("/api/v1/packages:plan", json={"package": package}, headers=auth(key))
    assert plan.status_code == 200, plan.text
    plan_hash: str = plan.json()["planHash"]
    applied = await client.post(
        "/api/v1/packages:apply",
        json={"package": package, "planHash": plan_hash},
        headers=auth(key),
    )
    assert applied.status_code == 200, applied.text
    return plan_hash


async def _get(client: httpx.AsyncClient, api_key: str, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params, headers=auth(api_key))
    assert response.status_code == 200, response.text
    return response.json()


async def _record(
    client: httpx.AsyncClient,
    key: str,
    objects: list[tuple[str, str]],
    *,
    version: str = "1.0.0",
    install_hash: str | None = "sha256:" + "a" * 64,
) -> httpx.Response:
    return await client.post(
        "/api/v1/packages:record",
        json={
            "package": {"key": PACKAGE, "version": version},
            "installHash": install_hash,
            "objects": [{"kind": kind, "key": k} for kind, k in objects],
        },
        headers=auth(key),
    )


async def _task_type(client: httpx.AsyncClient, key: str, type_key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": type_key,
            "displayName": type_key.title(),
            "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _rule(client: httpx.AsyncClient, key: str, rule_key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/rules",
        json={
            "key": rule_key,
            "trigger": {"kind": "observation", "type": "sample.appeared"},
            "action": {
                "kind": "ensure_work",
                "taskType": "intake",
                "dedupKeyTemplate": "sample:{{payload.data.id}}",
                "fields": {"title": "Sample {{payload.data.id}}"},
            },
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_an_apply_links_its_objects_and_the_next_version_moves_the_link(
    client: httpx.AsyncClient,
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    manual = await client.post(
        "/api/v1/calendars",
        json={"key": "by-hand", "spec": CALENDAR["spec"]},
        headers=auth(key),
    )
    assert manual.status_code == 201, manual.text
    assert manual.json()["package"] is None

    first = await _apply(client, key, "1.0.0")
    card = await _get(client, key, "/api/v1/calendars/ru")
    assert card["package"]["key"] == PACKAGE
    assert (card["package"]["version"], card["package"]["installHash"]) == ("1.0.0", first)
    assert card["package"]["installedAt"]
    assert (await _get(client, key, "/api/v1/calendars/by-hand"))["package"] is None

    listed = await _get(client, key, "/api/v1/calendars")
    assert {item["key"]: item["package"] is not None for item in listed["items"]} == {
        "by-hand": False,
        "ru": True,
    }
    only = await _get(client, key, "/api/v1/calendars", package=PACKAGE)
    assert [item["key"] for item in only["items"]] == ["ru"]
    assert (await _get(client, key, "/api/v1/calendars", package="other"))["items"] == []

    # The calendar is unchanged, the package is not: the link follows the package.
    second = await _apply(client, key, "1.1.0")
    assert second != first
    moved = (await _get(client, key, "/api/v1/calendars/ru@1"))["package"]
    assert (moved["version"], moved["installHash"]) == ("1.1.0", second)
    assert (await _get(client, key, "/api/v1/calendars/ru"))["version"] == 1

    # A version published by hand stays in the package of its key.
    spec = copy.deepcopy(CALENDAR["spec"])
    spec["displayName"] = "Changed by hand"
    by_hand = await client.post(
        "/api/v1/calendars", json={"key": "ru", "spec": spec}, headers=auth(key)
    )
    assert by_hand.status_code == 201, by_hand.text
    assert by_hand.json()["version"] == 2
    assert by_hand.json()["package"]["version"] == "1.1.0"


async def test_the_installer_records_what_it_applied(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    intake = await _task_type(client, key, "intake")
    await _task_type(client, key, "by-hand")
    rule = await _rule(client, key, "intake-appeared")
    role = await create_role(client, key, "reviewer")
    workspace = await create_workspace(client, key, "team")
    team_role = await create_role(client, key, "reviewer", workspace_id=workspace["id"])
    await create_capability(client, key, "review")
    await register_skill(client, key, "summarize", version="1.0.0")
    assert intake["package"] is None and rule["package"] is None

    objects = [
        ("WorkRule", "intake-appeared"),
        ("TaskType", "intake"),
        ("Role", "reviewer"),
        ("Capability", "review"),
        ("Skill", "summarize"),
    ]
    recorded = await _record(client, key, objects)
    assert recorded.status_code == 200, recorded.text
    body = recorded.json()
    assert body["package"] == {"key": PACKAGE, "version": "1.0.0"}
    # In the order of the catalog kinds, as the installer applies them.
    assert [(o["kind"], o["key"]) for o in body["recorded"]] == [
        ("TaskType", "intake"),
        ("Role", "reviewer"),
        ("Capability", "review"),
        ("Skill", "summarize"),
        ("WorkRule", "intake-appeared"),
    ]

    types = await _get(client, key, "/api/v1/task-types")
    by_key = {item["key"]: item["package"] for item in types["items"]}
    assert by_key["by-hand"] is None
    assert by_key["intake"] == {
        "key": PACKAGE,
        "version": "1.0.0",
        "installHash": "sha256:" + "a" * 64,
        "installedAt": by_key["intake"]["installedAt"],
    }
    filtered = await _get(client, key, "/api/v1/task-types", package=PACKAGE)
    assert [item["key"] for item in filtered["items"]] == ["intake"]
    card = await _get(client, key, f"/api/v1/task-types/{intake['id']}")
    assert card["package"]["key"] == PACKAGE

    rules = await _get(client, key, "/api/v1/rules", package=PACKAGE)
    assert [item["key"] for item in rules["items"]] == ["intake-appeared"]
    assert (await _get(client, key, f"/api/v1/rules/{rule['id']}"))["package"]["key"] == PACKAGE

    # A package brings tenant roles: the workspace role of the same slug is not its.
    roles = await _get(client, key, "/api/v1/roles", package=PACKAGE)
    assert [item["id"] for item in roles["items"]] == [role["id"]]
    assert (await _get(client, key, f"/api/v1/roles/{team_role['id']}"))["package"] is None
    capabilities = await _get(client, key, "/api/v1/capabilities", package=PACKAGE)
    assert [item["name"] for item in capabilities["items"]] == ["review"]
    skills = await _get(client, key, "/api/v1/skills", package=PACKAGE)
    assert [item["name"] for item in skills["items"]] == ["summarize"]
    assert (await _get(client, key, "/api/v1/skills/summarize"))["package"]["key"] == PACKAGE

    # The next version of the package, the task type unchanged: the link moves.
    again = await _record(client, key, [("TaskType", "intake")], version="1.1.0", install_hash=None)
    assert again.status_code == 200, again.text
    moved = (await _get(client, key, f"/api/v1/task-types/{intake['id']}"))["package"]
    assert (moved["version"], moved["installHash"]) == ("1.1.0", None)
    # A new version of the key published by hand is still the package's object.
    await _task_type(client, key, "intake")
    versions = await _get(client, key, "/api/v1/task-types", key="intake")
    assert [item["package"]["version"] for item in versions["items"]] == ["1.1.0", "1.1.0"]


async def test_a_record_names_only_objects_the_catalog_has(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    await _task_type(client, key, "intake")

    missing = await _record(client, key, [("TaskType", "intake"), ("TaskType", "absent")])
    assert missing.status_code == 422, missing.text
    error = missing.json()["error"]
    assert error["code"] == "unknown_object"
    assert error["details"]["objects"] == [{"kind": "TaskType", "key": "absent"}]
    # Nothing is linked: the record is all or nothing.
    assert (await _get(client, key, "/api/v1/task-types", package=PACKAGE))["items"] == []

    # Processes and calendars are linked by the apply; other kinds are not the core's.
    for kind in ("Process", "Calendar", "NotificationRule"):
        refused = await _record(client, key, [(kind, "ru")])
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["code"] == "invalid_request"


async def test_a_record_needs_packages_plan_and_the_right_of_the_kind(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _task_type(client, admin_key, "intake")
    _, planner = await create_agent_with_key(
        client, admin_key, name="planner", permissions=["packages.plan"]
    )
    _, installer = await create_agent_with_key(
        client, admin_key, name="installer", permissions=["packages.plan", "task_types.manage"]
    )
    _, types_only = await create_agent_with_key(
        client, admin_key, name="types", permissions=["task_types.manage"]
    )
    for denied in (planner, types_only):
        response = await _record(client, denied, [("TaskType", "intake")])
        assert response.status_code == 403, response.text
    assert (await _record(client, installer, [("TaskType", "intake")])).status_code == 200


async def test_an_agent_published_with_its_package_is_linked(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    spec = {
        "displayName": "Helper",
        "identity": {"kind": "service", "permissions": ["tasks.read"]},
        "placement": "none",
    }
    packaged = await client.post(
        "/api/v1/agents",
        json={"key": "helper", "spec": spec, "package": {"key": PACKAGE, "version": "2.0.0"}},
        headers=auth(key),
    )
    assert packaged.status_code == 201, packaged.text
    assert packaged.json()["package"]["version"] == "2.0.0"
    manual = await client.post(
        "/api/v1/agents", json={"key": "by-hand", "spec": spec}, headers=auth(key)
    )
    assert manual.status_code == 201, manual.text
    assert manual.json()["package"] is None

    card = await _get(client, key, "/api/v1/agents/helper")
    assert (card["package"]["key"], card["package"]["installHash"]) == (PACKAGE, None)
    listed = await _get(client, key, "/api/v1/agents", package=PACKAGE)
    assert [item["key"] for item in listed["items"]] == ["helper"]
    with_status = await _get(client, key, "/api/v1/agents", include="status")
    assert {item["key"]: item["package"] is not None for item in with_status["items"]} == {
        "helper": True,
        "by-hand": False,
    }


@pytest.fixture
def alembic_config(migrated_database: str) -> Any:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_the_migration_goes_down_and_up_and_links_packaged_agents(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    spec = {
        "displayName": "Helper",
        "identity": {"kind": "service", "permissions": ["tasks.read"]},
        "placement": "none",
    }
    for body in (
        {"key": "helper", "spec": spec, "package": {"key": PACKAGE, "version": "1.0.0"}},
        {"key": "helper", "spec": {**spec, "displayName": "Helper 2"}},
        {"key": "by-hand", "spec": spec},
    ):
        response = await client.post("/api/v1/agents", json=body, headers=auth(key))
        assert response.status_code in (200, 201), response.text
    await _task_type(client, key, "intake")
    assert (await _record(client, key, [("TaskType", "intake")])).status_code == 200
    await _apply(client, key, "1.0.0")

    alembic_command.downgrade(alembic_config, BEFORE_LINKS)
    with sync_engine.connect() as connection:
        kinds = connection.execute(text("SELECT kind FROM package_objects")).scalars().all()
    assert kinds == ["Calendar"]

    alembic_command.upgrade(alembic_config, "head")
    with sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT kind, key, package_key, package_version FROM package_objects ORDER BY kind"
            )
        ).all()
    # The applied calendar lost its package version; the agent is linked by its revisions.
    assert [tuple(row) for row in rows] == [
        ("Agent", "helper", PACKAGE, "1.0.0"),
        ("Calendar", "ru", PACKAGE, None),
    ]
    assert (await _get(client, key, "/api/v1/agents/by-hand"))["package"] is None
    assert uuid.UUID((await _get(client, key, "/api/v1/agents/helper"))["id"])
