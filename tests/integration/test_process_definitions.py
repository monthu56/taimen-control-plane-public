"""Process definitions at work (CP-ADR-0074 §1, §2; process-packages P006).

A version is published once: the same version with the same hash again is
``200`` without a write, with other content — or not above the latest —
``409 process_version_conflict``. A version passes the check of the kind
(shape, then language) against the tenant's catalog; a finding of class
error refuses it with ``422 invalid_process`` and the findings in
``details.problems``, warnings stay with the version. Reading is by ``key``
or ``key@version``, the list gives the latest version of each key, the
versions of a key come newest first without their spec.
"""

import copy
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.api.v1.processes import install_process
from control_plane.application.authorization import AuthContext
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from tests.helpers import auth, create_agent_with_key, create_workspace, do_bootstrap
from tests.unit.test_process_contract import _yaml12_loader

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"


def _document(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.load(path.read_text("utf-8"), Loader=_yaml12_loader())
    return document


SAMPLE = _document(FIXTURES / "sample.process.yaml")
CALENDAR = yaml.safe_load((FIXTURES / "ru-2024.calendar.yaml").read_text("utf-8"))
AGENT = "sample-process"


def _spec(**changes: Any) -> dict[str, Any]:
    spec = copy.deepcopy(SAMPLE["spec"])
    spec.update(changes)
    return spec


async def _catalog(
    client: httpx.AsyncClient, key: str, permissions: list[str] | None = None
) -> None:
    """What the sample refers to: its identity agent, task type, skill and calendar."""
    responses = [
        await client.post(
            "/api/v1/agents",
            json={
                "key": AGENT,
                "spec": {
                    "displayName": "Sample process",
                    "identity": {"kind": "service", "permissions": permissions or ["tasks.read"]},
                    "placement": "none",
                },
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/task-types",
            json={
                "key": "review",
                "displayName": "Review",
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
                "fieldSchema": {"type": "object", "properties": {"decision": {"type": "string"}}},
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/skills",
            json={
                "name": "text.summarize",
                "version": "1",
                "protocol": "http",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "outputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}},
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/calendars", json={"key": "ru", "spec": CALENDAR["spec"]}, headers=auth(key)
        ),
    ]
    for response in responses:
        assert response.status_code in (200, 201), response.text


async def _setup(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    await _catalog(client, key)
    return key


async def _publish(
    client: httpx.AsyncClient, key: str, spec: dict[str, Any], process: str = "sample"
) -> httpx.Response:
    return await client.post(
        "/api/v1/process-definitions", json={"key": process, "spec": spec}, headers=auth(key)
    )


async def test_a_version_is_published_once_with_its_hash(client: httpx.AsyncClient) -> None:
    key = await _setup(client)

    first = await _publish(client, key, SAMPLE["spec"])
    assert first.status_code == 201, first.text
    body = first.json()
    assert (body["key"], body["version"], body["latestVersion"]) == ("sample", 1, 1)
    assert body["definitionHash"].startswith("sha256:")
    assert body["identityAgent"] == AGENT
    assert body["owner"] == [{"role": "lead"}]
    assert body["expressionProfile"] == "cp/1"
    assert body["warnings"] == []
    assert body["workspaceId"] is None
    assert body["spec"]["displayName"] == "Sample"

    # The same version again, even with keys in another order: nothing written.
    reordered = dict(reversed(list(copy.deepcopy(SAMPLE["spec"]).items())))
    again = await _publish(client, key, reordered)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == body["id"]

    # The same version with other content, and a version not above the latest.
    changed = await _publish(client, key, _spec(displayName="Other"))
    assert changed.status_code == 409, changed.text
    error = changed.json()["error"]
    assert error["code"] == "process_version_conflict"
    assert error["details"]["definitionHash"] == body["definitionHash"]
    second = await _publish(client, key, _spec(version=3, displayName="Third"))
    assert second.status_code == 201, second.text
    lower = await _publish(client, key, _spec(version=2))
    assert lower.status_code == 409, lower.text
    assert lower.json()["error"]["details"]["latestVersion"] == 3
    # An older version is still recognised by its hash.
    assert (await _publish(client, key, SAMPLE["spec"])).status_code == 200

    events = await client.get(
        "/api/v1/events", params={"types": "process.definition_published"}, headers=auth(key)
    )
    assert events.status_code == 200, events.text
    items = events.json()["items"]
    assert [item["payload"]["version"] for item in items] == [1, 3]
    published = items[-1]
    assert (published["entityType"], published["entityId"]) == (
        "process_definition",
        second.json()["id"],
    )
    payload = published["payload"]
    assert (payload["previousVersion"], payload["identityAgent"]) == (1, AGENT)
    assert payload["definitionHash"] == second.json()["definitionHash"]
    elements = {element["id"]: element for element in payload["elements"]}
    assert elements["review"] == {
        "id": "review",
        "kind": "stage",
        "parent": None,
        "displayName": None,
        "governedBy": [],
    }
    assert elements["decide"]["parent"] == "review"
    assert elements["level"]["kind"] == "decision"


async def test_read_by_key_or_version_and_the_versions_of_a_key(
    client: httpx.AsyncClient,
) -> None:
    key = await _setup(client)
    _, reader_key = await create_agent_with_key(
        client, key, name="reader", permissions=["processes.read"]
    )
    for version in (1, 2, 3):
        response = await _publish(client, key, _spec(version=version, displayName=f"V{version}"))
        assert response.status_code == 201, response.text
    other = await _publish(client, key, SAMPLE["spec"], process="another")
    assert other.status_code == 201, other.text

    latest = await client.get("/api/v1/process-definitions/sample", headers=auth(reader_key))
    assert latest.status_code == 200, latest.text
    assert (latest.json()["version"], latest.json()["latestVersion"]) == (3, 3)
    pinned = await client.get("/api/v1/process-definitions/sample@1", headers=auth(reader_key))
    assert pinned.status_code == 200, pinned.text
    assert (pinned.json()["version"], pinned.json()["spec"]["displayName"]) == (1, "V1")
    for missing in ("sample@4", "sample@x", "sample@", "nothing"):
        response = await client.get(
            f"/api/v1/process-definitions/{missing}", headers=auth(reader_key)
        )
        assert response.status_code == 404, missing

    listed = await client.get("/api/v1/process-definitions", headers=auth(reader_key))
    assert listed.status_code == 200, listed.text
    assert [(i["key"], i["version"]) for i in listed.json()["items"]] == [
        ("another", 1),
        ("sample", 3),
    ]
    only = await client.get(
        "/api/v1/process-definitions", params={"key": "sample"}, headers=auth(reader_key)
    )
    assert [i["key"] for i in only.json()["items"]] == ["sample"]

    page = await client.get(
        "/api/v1/process-definitions/sample/versions",
        params={"limit": 2},
        headers=auth(reader_key),
    )
    assert page.status_code == 200, page.text
    items = page.json()["items"]
    assert [(i["version"], i["latestVersion"]) for i in items] == [(3, 3), (2, 3)]
    assert "spec" not in items[0]
    rest = await client.get(
        "/api/v1/process-definitions/sample/versions",
        params={"limit": 2, "cursor": page.json()["nextCursor"]},
        headers=auth(reader_key),
    )
    assert [i["version"] for i in rest.json()["items"]] == [1]
    assert rest.json()["nextCursor"] is None
    missing = await client.get(
        "/api/v1/process-definitions/nothing/versions", headers=auth(reader_key)
    )
    assert missing.status_code == 404


async def test_the_list_filters_by_regulation_and_workspace(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    workspace = await create_workspace(client, key, "tenders")
    assert (await _publish(client, key, SAMPLE["spec"])).status_code == 201
    scoped = await _publish(
        client,
        key,
        _spec(workspaceId=workspace["id"], governedBy=[{"document": "regulation:other"}]),
        process="scoped",
    )
    assert scoped.status_code == 201, scoped.text
    assert scoped.json()["workspaceId"] == workspace["id"]

    async def keys(**params: str) -> list[str]:
        response = await client.get("/api/v1/process-definitions", params=params, headers=auth(key))
        assert response.status_code == 200, response.text
        return [item["key"] for item in response.json()["items"]]

    assert await keys(governedBy="regulation:sample") == ["sample"]
    assert await keys(governedBy="regulation:other") == ["scoped"]
    assert await keys(governedBy="regulation:none") == []
    assert await keys(workspaceId=workspace["id"]) == ["scoped"]


def _with_step_regulation(document: str, **changes: Any) -> dict[str, Any]:
    """The sample with ``document`` named by a step only, not by the process."""
    spec = _spec(governedBy=[], **changes)
    step = spec["stages"][0]["steps"][2]
    step["governedBy"] = [{"document": document, "section": "5.2"}]
    return spec


async def test_governed_by_returns_exactly_the_processes_that_refer_to_the_document(
    client: httpx.AsyncClient,
) -> None:
    """CP-ADR-0076 §7: the processes a changed regulation concerns, and where their work goes."""
    key = await _setup(client)
    workspace = await create_workspace(client, key, "tenders")
    published = [
        # A step names the regulation: the process is governed by it.
        await _publish(
            client,
            key,
            _with_step_regulation("regulation:procurement", workspaceId=workspace["id"]),
            process="purchase",
        ),
        # The first version named it, the latest does not: not any more.
        await _publish(
            client, key, _with_step_regulation("regulation:procurement"), process="retired"
        ),
        await _publish(
            client,
            key,
            _with_step_regulation("regulation:other", version=2),
            process="retired",
        ),
        # Another document, and a key that only starts like it.
        await _publish(client, key, SAMPLE["spec"], process="unrelated"),
        await _publish(
            client,
            key,
            _with_step_regulation("regulation:procurement-archive"),
            process="prefix",
        ),
    ]
    for response in published:
        assert response.status_code == 201, response.text

    response = await client.get(
        "/api/v1/process-definitions",
        params={"governedBy": "regulation:procurement"},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert (item["key"], item["version"]) == ("purchase", 1)
    # What regulation-drift files its task with: the version's workspace and owner.
    assert item["workspaceId"] == workspace["id"]
    assert item["owner"] == [{"role": "lead"}]
    assert item["spec"]["stages"][0]["steps"][2]["governedBy"] == [
        {"document": "regulation:procurement", "section": "5.2"}
    ]


async def test_an_invalid_definition_is_refused_with_its_findings(
    client: httpx.AsyncClient,
) -> None:
    key = await _setup(client)
    document = _document(FIXTURES / "invalid" / "unknown_data_field.process.yaml")
    response = await _publish(client, key, document["spec"])
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_process"
    assert error["details"]["problems"] == [
        {
            "code": "unknown_data_field",
            "severity": "error",
            "path": "/spec/stages/0/steps/0/output/as/decison",
            "file": None,
            "line": None,
            "message": "data has no field decison",
            "hint": "did you mean decision?",
        }
    ]
    missing = await client.get("/api/v1/process-definitions/sample", headers=auth(key))
    assert missing.status_code == 404

    # The shape comes from the catalog schema: a finding, not a 400 of the route.
    shape = await _publish(client, key, _spec(stages=[]))
    assert shape.status_code == 422, shape.text
    problems = shape.json()["error"]["details"]["problems"]
    assert [(p["code"], p["path"]) for p in problems] == [("schema_violation", "/spec/stages")]


async def test_warnings_do_not_refuse_the_version(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    spec = _spec()
    del spec["owner"]
    response = await _publish(client, key, spec)
    assert response.status_code == 201, response.text
    assert response.json()["owner"] is None
    assert [(w["code"], w["path"], w["severity"]) for w in response.json()["warnings"]] == [
        ("process_owner_missing", "/spec/owner", "warning")
    ]
    read = await client.get("/api/v1/process-definitions/sample", headers=auth(key))
    assert read.json()["warnings"] == response.json()["warnings"]


async def test_the_identity_is_required_and_lends_no_more_than_the_publisher_holds(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _catalog(client, admin_key, permissions=["tasks.read", "tasks.write"])
    _, writer_key = await create_agent_with_key(
        client, admin_key, name="writer", permissions=["processes.write", "tasks.read"]
    )

    spec = _spec()
    del spec["identity"]
    missing = await _publish(client, admin_key, spec)
    assert missing.status_code == 422, missing.text
    problems = missing.json()["error"]["details"]["problems"]
    assert [(p["code"], p["path"]) for p in problems] == [
        ("process_identity_required", "/spec/identity")
    ]
    unknown = await _publish(client, admin_key, _spec(identity={"agent": "nobody"}))
    assert unknown.status_code == 422, unknown.text
    problems = unknown.json()["error"]["details"]["problems"]
    assert [(p["code"], p["path"]) for p in problems] == [("unknown_agent", "/spec/identity/agent")]

    # The agent may write tasks, the publisher may not: no escalation by a process.
    escalation = await _publish(client, writer_key, SAMPLE["spec"])
    assert escalation.status_code == 403, escalation.text
    assert escalation.json()["error"]["code"] == "permission_escalation"
    assert (await _publish(client, admin_key, SAMPLE["spec"])).status_code == 201


async def test_publishing_and_reading_need_their_permissions(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    _, outsider_key = await create_agent_with_key(
        client, key, name="outsider", permissions=["tasks.read"]
    )
    assert (await _publish(client, key, SAMPLE["spec"])).status_code == 201
    assert (await _publish(client, outsider_key, _spec(version=2))).status_code == 403
    for path in (
        "/api/v1/process-definitions",
        "/api/v1/process-definitions/sample",
        "/api/v1/process-definitions/sample/versions",
    ):
        response = await client.get(path, headers=auth(outsider_key))
        assert response.status_code == 403, path


async def test_a_package_object_installs_like_the_route(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await _catalog(client, admin_key)
    ctx = AuthContext(
        tenant_id=uuid.UUID(boot["tenant"]["id"]),
        principal_id=uuid.UUID(boot["adminPrincipal"]["id"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset({Permission.PROCESSES_WRITE.value, Permission.TASKS_READ.value}),
    )
    async with app.state.session_factory() as db:
        view = await install_process(db, ctx, SAMPLE)
        await db.commit()
    assert (view.created, view.row.version) == (True, 1)
    route = await _publish(client, admin_key, SAMPLE["spec"])
    assert route.status_code == 200, route.text
    assert route.json()["definitionHash"] == view.row.definition_hash

    async with app.state.session_factory() as db:
        with pytest.raises(ValidationError):
            await install_process(db, ctx, {**SAMPLE, "kind": "Calendar"})


async def test_versions_are_immutable(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    key = await _setup(client)
    assert (await _publish(client, key, SAMPLE["spec"])).status_code == 201
    for statement in (
        "UPDATE process_definitions SET spec = '{}'::jsonb",
        "DELETE FROM process_definitions",
    ):
        with pytest.raises(Exception, match="immutable"), sync_engine.begin() as connection:
            connection.execute(text(statement))
