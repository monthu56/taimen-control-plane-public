"""``POST /packages:plan`` and ``POST /packages:apply`` at work (CP-ADR-0074 §11; P015).

The acceptance of P015:

- a step renamed with a migration map while instances wait on it — the apply
  moves every one of them to the new version, on the same step (SC-006);
- the catalog changed between the plan and the apply — the apply refuses
  ``409 plan_stale``;
- the plan lists the sections of a regulation no element is governed by.

Plus the owner of a field (a person's change is kept), ``migration_required``,
the rename of a process with its instances, and a plan that writes nothing.
"""

import copy
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from control_plane.application.context import graph
from control_plane.worker.main import Worker
from tests.fake_graph_memory import Edge, FakeGraphMemory, Node
from tests.helpers import auth, create_agent_with_key, create_workspace
from tests.integration.test_package_test import API_VERSION, CALENDAR, FIXTURES, snapshot
from tests.integration.test_process_instances import (
    AGENT,
    _complete,
    _events,
    _instance,
    _journal,
    _open,
    _setup,
    worker,
)

__all__ = ["worker"]

PROCESS = "sample-plan"
REGULATION = "regulation:purchasing"


def _spec(
    admin: str,
    *,
    version: int = 1,
    step: str = "review",
    display: str = "Sample plan",
    migrations: list[dict[str, Any]] | None = None,
    governed: list[dict[str, Any]] | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    review: dict[str, Any] = {
        "id": step,
        "human": {
            "taskType": "review",
            "assign": [{"principal": admin}],
            "due": "P3D",
            "escalations": [{"after": "due", "action": "notify", "to": [{"principal": admin}]}],
        },
        "output": {"as": {"decision": "step.result.decision"}},
    }
    if governed:
        review["governedBy"] = governed
    spec: dict[str, Any] = {
        "version": version,
        "displayName": display,
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {"type": "object", "properties": {"decision": {"type": "string"}}},
        "start": {"on": {"observation": "sample.opened"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "steps": [review, {"id": "done", "complete": {"outcome": "reviewed"}}],
            }
        ],
    }
    if migrations is not None:
        spec["migrations"] = migrations
    if workspace is not None:
        spec["workspaceId"] = workspace
    return spec


def _package(
    spec: dict[str, Any],
    *,
    key: str = PROCESS,
    renames: list[dict[str, Any]] | None = None,
    calendar: bool = False,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "sample-plan",
        "spec": {"version": "1.0.0", "displayName": "Sample plan"},
    }
    if renames:
        manifest["spec"]["renames"] = renames
    process = {"apiVersion": API_VERSION, "kind": "Process", "key": key, "spec": spec}
    files = [
        ("package.yaml", yaml.safe_dump(manifest)),
        ("processes/sample.yaml", yaml.safe_dump(process, sort_keys=False)),
    ]
    if calendar:
        files.append(("calendars/ru.yaml", CALENDAR))
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def _plan(
    client: httpx.AsyncClient, key: str, package: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/packages:plan", json={"package": package, **extra}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    out: dict[str, Any] = response.json()
    return out


async def _apply(
    client: httpx.AsyncClient, key: str, package: dict[str, Any], plan_hash: str, **extra: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/packages:apply",
        json={"package": package, "planHash": plan_hash, **extra},
        headers=auth(key),
    )


async def _plan_and_apply(
    client: httpx.AsyncClient, key: str, package: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    plan = await _plan(client, key, package, **extra)
    applied = await _apply(client, key, package, plan["planHash"], **extra)
    assert applied.status_code == 200, applied.text
    out: dict[str, Any] = applied.json()
    return out


async def _start(client: httpx.AsyncClient, key: str, number: str, process: str = PROCESS) -> str:
    response = await client.post(
        "/api/v1/process-instances",
        json={"process": process, "key": number},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    instance_id: str = response.json()["id"]
    return instance_id


def _errors(plan: dict[str, Any]) -> list[str]:
    return [p["code"] for p in plan["problems"] if p["severity"] == "error"]


async def test_a_renamed_step_with_a_map_moves_every_open_instance_onto_it(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    first = await _plan(client, key, _package(_spec(admin), calendar=True))
    assert [(c["kind"], c["key"], c["action"]) for c in first["changes"]] == [
        ("Calendar", "ru", "create"),
        ("Process", PROCESS, "create"),
    ]
    assert first["package"] == {"key": "sample-plan", "version": "1.0.0"}
    applied = await _apply(client, key, _package(_spec(admin), calendar=True), first["planHash"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] == [
        {"kind": "Calendar", "key": "ru", "action": "create", "version": 1},
        {"kind": "Process", "key": PROCESS, "action": "create", "version": 1},
    ]
    instances = [await _start(client, key, f"N-{n}") for n in range(3)]

    renamed = _package(
        _spec(
            admin,
            version=2,
            step="check",
            migrations=[{"from": 1, "to": 2, "policy": "migrate", "map": {"review": "check"}}],
        ),
        calendar=True,
    )
    plan = await _plan(client, key, renamed)
    assert _errors(plan) == []
    changes = {c["key"]: c for c in plan["changes"]}
    assert changes["ru"]["action"] == "unchanged"
    process = changes[PROCESS]
    assert process["action"] == "update"
    assert {f["path"] for f in process["fields"]} == {
        "/spec/version",
        "/spec/stages",
        "/spec/migrations",
    }
    assert all(f["owner"] == "package" and f["applies"] for f in process["fields"])
    [processes] = plan["processes"]
    assert (processes["fromVersion"], processes["toVersion"]) == (1, 2)
    assert processes["instances"] == [
        {"version": 1, "open": 3, "fate": "migrate", "migrationRequired": False}
    ]
    # The behaviour: the renamed step decides under another id on every instance.
    assert processes["behaviour"]["replayed"] == 3
    assert processes["behaviour"]["diverged"] == 3
    assert plan["catalogEtag"].startswith("sha256:") and plan["planHash"].startswith("sha256:")

    applied = await _apply(client, key, renamed, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"][1] == {
        "kind": "Process",
        "key": PROCESS,
        "action": "update",
        "version": 2,
    }
    assert applied.json()["catalogEtag"] != plan["catalogEtag"]

    for instance_id in instances:
        instance = await _instance(client, key, instance_id)
        assert instance["definitionVersion"] == 2
        assert instance["status"] == "running"
        assert _open(instance, "check")["taskId"] is not None
        # The escalation and the deadline of the step (engine revision 2) moved onto check.
        assert [t["element"] for t in instance["timers"]] == ["check", "check"]
        journal = await _journal(client, key, instance_id)
        [moved] = [e for e in journal if e["kind"] == "migration"]
        # The instance runs under the engine revision of version 2 from here on.
        assert moved["data"]["toVersion"] == 2
        assert moved["data"]["engineRevision"] == 2
    migrated = await _events(client, key, "process.migrated")
    assert sorted(e["payload"]["instanceId"] for e in migrated) == sorted(instances)
    assert all(
        e["payload"]["fromVersion"] == 1
        and e["payload"]["version"] == 2
        and e["payload"]["map"] == {"review": "check"}
        and e["payload"]["policy"] == "migrate"
        for e in migrated
    )

    # The task opened on version 1 answers the step of version 2.
    instance = await _instance(client, key, instances[0])
    await _complete(client, key, _open(instance, "check")["taskId"], {"decision": "go"})
    await worker.run_once()
    closed = await _instance(client, key, instances[0])
    assert (closed["status"], closed["outcome"], closed["data"]) == (
        "completed",
        "reviewed",
        {"decision": "go"},
    )

    # The same package again: nothing to do.
    again = await _plan(client, key, renamed)
    assert [c["action"] for c in again["changes"]] == ["unchanged", "unchanged"]
    assert again["processes"] == []


async def test_an_element_gone_under_open_instances_needs_a_migration(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, _package(_spec(admin)))
    instance_id = await _start(client, key, "N-1")

    gone = _package(_spec(admin, version=2, step="check"))
    plan = await _plan(client, key, gone)
    assert "migration_required" in _errors(plan)
    [problem] = [p for p in plan["problems"] if p["code"] == "migration_required"]
    assert problem["file"] == "processes/sample.yaml"
    assert "review" in problem["message"]
    assert plan["processes"][0]["instances"] == [
        {"version": 1, "open": 1, "fate": "unaffected", "migrationRequired": True}
    ]
    refused = await _apply(client, key, gone, plan["planHash"])
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "migration_required"

    # pin: the instances finish on version 1, nothing is required.
    pinned = _package(
        _spec(admin, version=2, step="check", migrations=[{"from": 1, "to": 2, "policy": "pin"}])
    )
    plan = await _plan(client, key, pinned)
    assert _errors(plan) == []
    assert plan["processes"][0]["instances"][0]["fate"] == "pin"
    await _plan_and_apply(client, key, pinned)
    instance = await _instance(client, key, instance_id)
    assert instance["definitionVersion"] == 1
    assert _open(instance, "review")["taskId"] is not None


async def test_the_catalog_changed_between_plan_and_apply_is_plan_stale(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, _package(_spec(admin)))
    next_version = _package(_spec(admin, version=2, display="Sample plan v2"))
    before = snapshot(sync_engine)
    plan = await _plan(client, key, next_version)
    assert snapshot(sync_engine) == before, "a plan writes nothing"

    # Someone publishes a version meanwhile.
    published = await client.post(
        "/api/v1/process-definitions",
        json={"key": PROCESS, "spec": _spec(admin, version=2, display="By hand")},
        headers=auth(key),
    )
    assert published.status_code == 201, published.text
    stale = await _apply(client, key, next_version, plan["planHash"])
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "plan_stale"
    assert error["details"]["planHash"] == plan["planHash"]
    assert error["details"]["currentPlanHash"] != plan["planHash"]

    # A package that is not the one planned is stale as well.
    fresh = await _plan(client, key, _package(_spec(admin, version=3)))
    other = await _apply(client, key, _package(_spec(admin, version=4)), fresh["planHash"])
    assert other.status_code == 409 and other.json()["error"]["code"] == "plan_stale"


async def test_a_field_a_person_changed_is_kept_unless_overwritten(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, _package(_spec(admin)))
    by_hand = await client.post(
        "/api/v1/process-definitions",
        json={"key": PROCESS, "spec": _spec(admin, version=2, display="Named by hand")},
        headers=auth(key),
    )
    assert by_hand.status_code == 201, by_hand.text

    package = _package(_spec(admin, version=3, display="Sample plan"))
    plan = await _plan(client, key, package)
    [change] = plan["changes"]
    fields = {f["path"]: f for f in change["fields"]}
    assert fields["/spec/displayName"] == {
        "path": "/spec/displayName",
        "before": "Named by hand",
        "after": "Sample plan",
        "owner": "console",
        "applies": False,
    }
    assert fields["/spec/version"]["owner"] == "package"
    await _apply(client, key, package, plan["planHash"])
    kept = await client.get(f"/api/v1/process-definitions/{PROCESS}", headers=auth(key))
    assert (kept.json()["version"], kept.json()["displayName"]) == (3, "Named by hand")

    # The person still owns the field; the flag overwrites it.
    package = _package(_spec(admin, version=4, display="Sample plan"))
    plan = await _plan(client, key, package, overwriteConsole=True)
    [change] = plan["changes"]
    field = next(f for f in change["fields"] if f["path"] == "/spec/displayName")
    assert (field["owner"], field["applies"]) == ("console", True)
    # The flag is part of the plan: an apply without it is another plan.
    without = await _apply(client, key, package, plan["planHash"])
    assert without.status_code == 409, without.text
    applied = await _apply(client, key, package, plan["planHash"], overwriteConsole=True)
    assert applied.status_code == 200, applied.text
    now = await client.get(f"/api/v1/process-definitions/{PROCESS}", headers=auth(key))
    assert now.json()["displayName"] == "Sample plan"


async def test_a_renamed_process_takes_its_instances_and_the_old_key_starts_nothing(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, _package(_spec(admin)))
    instance_id = await _start(client, key, "N-1")

    moved = _package(
        _spec(admin, version=2, migrations=[{"from": 1, "to": 2, "policy": "migrate"}]),
        key="sample-renamed",
        renames=[{"kind": "Process", "from": PROCESS, "to": "sample-renamed"}],
    )
    plan = await _plan(client, key, moved)
    assert _errors(plan) == []
    [change] = plan["changes"]
    assert (change["key"], change["action"], change["renamedFrom"]) == (
        "sample-renamed",
        "rename",
        PROCESS,
    )
    assert plan["processes"][0]["instances"] == [
        {"version": 1, "open": 1, "fate": "migrate", "migrationRequired": False}
    ]
    assert plan["processes"][0]["behaviour"] == {"replayed": 1, "diverged": 0, "instanceIds": []}
    applied = await _apply(client, key, moved, plan["planHash"])
    assert applied.status_code == 200, applied.text

    instance = await _instance(client, key, instance_id)
    assert (instance["definitionKey"], instance["definitionVersion"]) == ("sample-renamed", 2)
    refused = await client.post(
        "/api/v1/process-instances", json={"process": PROCESS, "key": "N-2"}, headers=auth(key)
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "process_retired"
    await _start(client, key, "N-2", "sample-renamed")
    old = await client.get(f"/api/v1/process-definitions/{PROCESS}", headers=auth(key))
    assert old.json()["status"] == "retired"
    assert old.json()["retired"]["reason"] == "renamed by package sample-plan"

    # The rename is done: the same package plans nothing more.
    again = await _plan(client, key, moved)
    assert [(c["action"], c["renamedFrom"]) for c in again["changes"]] == [("unchanged", None)]

    # A version published by hand brings the old key back; the rename is a conflict then.
    revived = await client.post(
        "/api/v1/process-definitions",
        json={"key": PROCESS, "spec": _spec(admin, version=2)},
        headers=auth(key),
    )
    assert revived.status_code == 201, revived.text
    await _start(client, key, "N-3")
    conflict = await _plan(client, key, moved)
    assert "rename_target_exists" in _errors(conflict)


async def test_a_process_retired_between_plan_and_apply_is_plan_stale(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, _package(_spec(admin)))
    next_version = _package(_spec(admin, version=2, display="Sample plan v2"))
    plan = await _plan(client, key, next_version)

    retired = await client.post(
        f"/api/v1/process-definitions/{PROCESS}:retire",
        json={"reason": "replaced"},
        headers=auth(key),
    )
    assert retired.status_code == 200, retired.text
    stale = await _apply(client, key, next_version, plan["planHash"])
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "plan_stale"

    # The package publishing a new version brings the key back (CP-ADR-0074 Zh1).
    applied = await _plan_and_apply(client, key, next_version)
    assert applied["applied"][0]["action"] == "update"
    shown = await client.get(f"/api/v1/process-definitions/{PROCESS}", headers=auth(key))
    assert (shown.json()["status"], shown.json()["retired"]) == ("active", None)
    await _start(client, key, "N-1")


async def _retire(client: httpx.AsyncClient, key: str, path: str) -> None:
    response = await client.post(
        f"/api/v1/{path}:retire", json={"reason": "replaced"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text


def _actions(plan: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(c["kind"], c["key"]): c["action"] for c in plan["changes"]}


async def test_a_retired_process_and_its_calendar_installed_as_they_are_are_restored(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    spec = {**_spec(admin), "calendar": "ru"}
    package = _package(spec, calendar=True)
    await _plan_and_apply(client, key, package)
    await _retire(client, key, f"process-definitions/{PROCESS}")
    await _retire(client, key, "calendars/ru")

    # Without the calendar the restored process would need one out of use.
    alone = await _plan(client, key, _package(spec))
    assert _actions(alone) == {("Process", PROCESS): "restore"}
    assert [(p["code"], p["path"]) for p in alone["problems"] if p["severity"] == "error"] == [
        ("calendar_retired", "/spec/calendar")
    ]
    refused = await _apply(client, key, _package(spec), alone["planHash"])
    assert refused.status_code == 422, refused.text

    # The package installs both as they are: the plan says so, and the apply does it.
    plan = await _plan(client, key, package)
    assert _actions(plan) == {("Calendar", "ru"): "restore", ("Process", PROCESS): "restore"}
    assert _errors(plan) == []
    assert plan["processes"] == []
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert [(a["kind"], a["action"], a["version"]) for a in applied.json()["applied"]] == [
        ("Calendar", "restore", 1),
        ("Process", "restore", 1),
    ]
    calendar = await client.get("/api/v1/calendars/ru", headers=auth(key))
    assert (calendar.json()["status"], calendar.json()["version"]) == ("active", 1)
    shown = await client.get(f"/api/v1/process-definitions/{PROCESS}", headers=auth(key))
    assert (shown.json()["status"], shown.json()["version"]) == ("active", 1)
    await _start(client, key, "N-1")
    [process_event] = await _events(client, key, "process.definition_restored")
    assert process_event["entityType"] == "process_definition"
    assert process_event["payload"] == {
        "key": PROCESS,
        "latestVersion": 1,
        "packageKey": "sample-plan",
        "packageVersion": "1.0.0",
    }
    [calendar_event] = await _events(client, key, "calendar.restored")
    assert calendar_event["payload"]["key"] == "ru"

    # Back in use, the same package plans nothing more.
    again = await _plan(client, key, package)
    assert set(_actions(again).values()) == {"unchanged"}


async def test_a_retired_calendar_installed_as_it_is_serves_a_process_updated_to_it(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, _package(_spec(admin), calendar=True))
    await _retire(client, key, "calendars/ru")

    # The new version names the calendar; the package installs it as it is.
    package = _package({**_spec(admin, version=2), "calendar": "ru"}, calendar=True)
    plan = await _plan(client, key, package)
    assert _actions(plan) == {("Calendar", "ru"): "restore", ("Process", PROCESS): "update"}
    assert _errors(plan) == []
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    assert [(a["kind"], a["action"], a["version"]) for a in applied.json()["applied"]] == [
        ("Calendar", "restore", 1),
        ("Process", "update", 2),
    ]
    calendar = await client.get("/api/v1/calendars/ru", headers=auth(key))
    assert (calendar.json()["status"], calendar.json()["version"]) == ("active", 1)
    shown = await client.get(f"/api/v1/process-definitions/{PROCESS}", headers=auth(key))
    assert (shown.json()["status"], shown.json()["version"]) == ("active", 2)
    assert shown.json()["spec"]["calendar"] == "ru"
    [calendar_event] = await _events(client, key, "calendar.restored")
    assert calendar_event["payload"]["key"] == "ru"
    await _start(client, key, "N-1")

    # The calendar is needed again: it is not retired a second time.
    refused = await client.post(
        "/api/v1/calendars/ru:retire", json={"reason": "replaced"}, headers=auth(key)
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "calendar_in_use"


async def test_a_retired_process_installed_as_it_is_is_restored(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    package = _package(_spec(admin))
    await _plan_and_apply(client, key, package)
    await _retire(client, key, f"process-definitions/{PROCESS}")
    refused = await client.post(
        "/api/v1/process-instances", json={"process": PROCESS, "key": "N-1"}, headers=auth(key)
    )
    assert refused.json()["error"]["code"] == "process_retired"

    applied = await _plan_and_apply(client, key, package)
    assert applied["applied"] == [
        {"kind": "Process", "key": PROCESS, "action": "restore", "version": 1}
    ]
    versions = await client.get(
        f"/api/v1/process-definitions/{PROCESS}/versions", headers=auth(key)
    )
    assert [(v["version"], v["status"]) for v in versions.json()["items"]] == [(1, "active")]
    await _start(client, key, "N-1")


class RegulationMemory(FakeGraphMemory):
    """A regulation of three sections."""

    def __init__(self) -> None:
        super().__init__()
        sections = [f"{REGULATION}#{n}" for n in ("1", "2", "3")]
        self.nodes = {
            n.key: n
            for n in (
                Node(REGULATION, "regulation", "Purchasing"),
                *(Node(key, "regulation_section", key) for key in sections),
            )
        }
        self.edges = [
            Edge(key, "section_of", REGULATION, fact_id=f"f-{index}")
            for index, key in enumerate(sections)
        ]


@pytest.fixture
def memory(app: FastAPI) -> Any:
    graph._pack_patterns.clear()
    fake = RegulationMemory()
    app.state.context_provider = fake
    yield fake
    app.state.context_provider = None


async def test_the_plan_lists_the_sections_of_a_regulation_no_element_covers(
    client: httpx.AsyncClient, memory: RegulationMemory
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    workspace = await create_workspace(client, key, "purchasing")
    governed = [
        {"document": REGULATION, "section": "1"},
        {"document": REGULATION, "section": "9"},
    ]
    spec = _spec(admin, governed=governed, workspace=workspace["id"])
    spec["governedBy"] = [{"document": "regulation:absent"}]
    plan = await _plan(client, key, _package(spec))
    coverage = {c["document"]: c for c in plan["regulationCoverage"]}
    assert coverage[REGULATION] == {
        "document": REGULATION,
        "found": True,
        "covered": {"1": [f"{PROCESS}/review"], "9": [f"{PROCESS}/review"]},
        "uncovered": ["2", "3"],
    }
    assert coverage["regulation:absent"] == {
        "document": "regulation:absent",
        "found": False,
        "covered": {},
        "uncovered": [],
    }
    [unknown] = [p for p in plan["problems"] if p["code"] == "governed_by_unknown_section"]
    assert unknown["severity"] == "warning"
    assert unknown["path"] == "/spec/stages/0/steps/0/governedBy/1/section"
    request = memory.typed_requests[0]
    assert request["traverse"] == [
        {"relation": "section_of", "direction": "in", "depth": 1, "limit": 200}
    ]
    assert request["allow_semantic"] is False


async def test_plan_and_apply_need_their_permission_and_a_manifest(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    _, reader = await create_agent_with_key(
        client, key, name="reader", permissions=["processes.read"]
    )
    package = _package(_spec(admin))
    denied = await client.post(
        "/api/v1/packages:plan", json={"package": package}, headers=auth(reader)
    )
    assert denied.status_code == 403, denied.text
    denied = await _apply(client, reader, package, "sha256:" + "0" * 64)
    assert denied.status_code == 403, denied.text

    # A planner without processes.write plans, but the apply is refused by the kind's right.
    _, planner = await create_agent_with_key(
        client, key, name="planner", permissions=["packages.plan"]
    )
    plan = await _plan(client, planner, package)
    refused = await _apply(client, planner, package, plan["planHash"])
    assert refused.status_code == 403, refused.text

    headless = copy.deepcopy(package)
    headless["files"] = [f for f in headless["files"] if f["path"] != "package.yaml"]
    plan = await _plan(client, key, headless)
    assert "package_manifest_missing" in _errors(plan)
    invalid = await _apply(client, key, headless, plan["planHash"])
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "invalid_package"


async def test_the_plan_checks_the_calendar_of_a_due(client: httpx.AsyncClient) -> None:
    """CP-ADR-0078 §1 (P009): the plan refuses what the publication would."""
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    spec = _spec(admin)
    spec["stages"][0]["steps"][0]["human"]["due"] = {"workhours": 8}
    here = "/spec/stages/0/steps/0/human/due/workhours"

    def problems(plan: dict[str, Any]) -> list[tuple[str, str]]:
        return [(p["code"], p["path"]) for p in plan["problems"] if p["severity"] == "error"]

    plan = await _plan(client, key, _package(spec, calendar=True))
    assert problems(plan) == [("sla_calendar_missing", here)]
    spec["calendar"] = "ru"
    plan = await _plan(client, key, _package(spec, calendar=True))
    assert problems(plan) == [("sla_calendar_without_hours", here)]
    package = _package(spec, calendar=True)
    with_hours = (FIXTURES / "ru-2025-2027.calendar.yaml").read_text(encoding="utf-8")
    for item in package["files"]:
        if item["path"] == "calendars/ru.yaml":
            item["content"] = with_hours
    assert problems(await _plan(client, key, package)) == []
