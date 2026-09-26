"""Executor instructions from the task type and the project (CP-ADR-0066).

The type version carries ``instructions`` (immutable, checked at publication),
the project carries ``settings.agentInstructions``; the core assembles them
after its platform contract into ``{layers, hash}``, hands the block out with
the run context and the working context, and pins the hash and the layer
versions on the run and on ``run.started``.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.domain.agent_instructions import (
    MAX_INSTRUCTIONS_BYTES,
    PLATFORM_CONTRACT_REF,
    PLATFORM_CONTRACT_VERSION,
)
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)

TYPE_KEY = "sdd-spec"
TYPE_TEXT = "Пиши спецификацию: требования, границы, критерии приёмки. Кода не пиши."
PROJECT_TEXT = "Конституция: сначала тест, потом код; документация на русском."


async def _type(client: httpx.AsyncClient, key: str, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"key": TYPE_KEY, "displayName": "Spec", **extra}
    return await client.post("/api/v1/task-types", json=body, headers=auth(key))


async def _project(client: httpx.AsyncClient, key: str, workspace_id: str) -> dict[str, Any]:
    template = await client.post(
        "/api/v1/project-templates",
        json={"key": "delivery", "displayName": "Delivery"},
        headers=auth(key),
    )
    assert template.status_code == 201, template.text
    created = await client.post(
        "/api/v1/projects",
        json={"workspaceId": workspace_id, "templateKey": "delivery"},
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    project = created.json()
    revision = await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions",
        json={"config": {"settings": {"agentInstructions": PROJECT_TEXT}}},
        headers=auth(key),
    )
    assert revision.status_code == 201, revision.text
    activated = await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions/1:activate",
        headers={**auth(key), "If-Match": f'"project-{project["version"]}"'},
    )
    assert activated.status_code == 200, activated.text
    return project


async def _run(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    session = await open_session(client, key)
    claim = (await claim_task(client, key, task_id, session["id"])).json()
    response = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _run_context(client: httpx.AsyncClient, key: str, run_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/runs/{run_id}/context", headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


# --- publication ------------------------------------------------------------------


async def test_instructions_are_published_with_the_version(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    first = await _type(client, key, instructions=TYPE_TEXT)
    assert first.status_code == 201, first.text
    assert first.json()["instructions"] == TYPE_TEXT
    read = await client.get(f"/api/v1/task-types/{first.json()['id']}", headers=auth(key))
    assert read.json()["instructions"] == TYPE_TEXT

    # Changing the instructions is the next version, never an edit.
    second = await _type(client, key, instructions=TYPE_TEXT + " Ссылайся на ADR.")
    assert second.json()["version"] == first.json()["version"] + 1
    plain = await _type(client, key)
    assert plain.json()["instructions"] == ""


@pytest.mark.parametrize(
    ("instructions", "code"),
    [
        ("Деплой через token=AKIAABCDEFGHIJKLMNOP", "secret_material_rejected"),
        ("x" * (MAX_INSTRUCTIONS_BYTES + 1), "instructions_too_large"),
    ],
)
async def test_bad_instructions_are_refused_at_publication(
    client: httpx.AsyncClient, instructions: str, code: str
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _type(client, key, instructions=instructions)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["details"]["field"] == "instructions"


async def test_instructions_are_immutable_like_the_rest_of_the_version(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await _type(client, key, instructions=TYPE_TEXT)).json()
    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_types SET instructions = '' WHERE id = :id"),
            {"id": created["id"]},
        )


async def test_secret_in_the_project_layer_is_refused(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, key, "platform")
    project = await _project(client, key, workspace["id"])
    response = await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions",
        json={"config": {"settings": {"agentInstructions": "password: hunter2hunter2hunter2"}}},
        headers=auth(key),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "secret_material_rejected"


# --- delivery and the trace on the run -----------------------------------------------


async def test_run_gets_the_layers_in_order_and_pins_their_hash(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin, "platform")
    project = await _project(client, admin, workspace["id"])
    task_type = (await _type(client, admin, instructions=TYPE_TEXT)).json()
    task = await create_task(
        client, admin, title="Spec it", typeKey=TYPE_KEY, workspaceId=workspace["id"]
    )
    _, agent = await create_agent_with_key(client, admin)

    run = await _run(client, agent, task["id"])
    context = await _run_context(client, agent, run["id"])

    block = context["instructions"]
    assert [(item["source"], item["ref"], item["version"]) for item in block["layers"]] == [
        ("platform", PLATFORM_CONTRACT_REF, PLATFORM_CONTRACT_VERSION),
        ("project", f"project:{project['id']}", 1),
        ("taskType", f"taskType:{TYPE_KEY}", task_type["version"]),
    ]
    assert block["layers"][1]["text"] == PROJECT_TEXT
    assert block["layers"][2]["text"] == TYPE_TEXT
    assert block["hash"].startswith("sha256:")

    # The run records what it was started under, and so does run.started.
    assert run["instructionsHash"] == block["hash"]
    assert run["instructionsRefs"]["layers"] == [
        {"source": item["source"], "ref": item["ref"], "version": item["version"]}
        for item in block["layers"]
    ]
    assert context["run"]["instructionsHash"] == block["hash"]
    events = (await client.get("/api/v1/events", headers=auth(agent))).json()["items"]
    started = next(e for e in events if e["type"] == "run.started" and e["entityId"] == run["id"])
    assert started["payload"]["instructionsHash"] == block["hash"]
    assert started["payload"]["instructionsRefs"][2]["version"] == task_type["version"]

    # The working context the runner reads at the start of work: the same block.
    working = await client.post(
        "/api/v1/context",
        json={"runId": run["id"], "includeMemory": False},
        headers=auth(agent),
    )
    assert working.status_code == 200, working.text
    assert working.json()["instructions"] == block


async def test_new_type_version_changes_the_hash(client: httpx.AsyncClient) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent = await create_agent_with_key(client, admin)
    await _type(client, admin, instructions=TYPE_TEXT)
    first = await create_task(client, admin, title="v1", typeKey=TYPE_KEY)
    await _type(client, admin, instructions=TYPE_TEXT + " Добавь раздел рисков.")
    second = await create_task(client, admin, title="v2", typeKey=TYPE_KEY)

    run1 = await _run(client, agent, first["id"])
    run2 = await _run(client, agent, second["id"])
    assert run1["instructionsHash"] != run2["instructionsHash"]
    assert run1["instructionsRefs"]["layers"][-1]["version"] == 1
    assert run2["instructionsRefs"]["layers"][-1]["version"] == 2


async def test_type_without_instructions_gets_the_platform_contract_alone(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent = await create_agent_with_key(client, admin)
    task = await create_task(client, admin, title="Plain")
    run = await _run(client, agent, task["id"])
    block = (await _run_context(client, agent, run["id"]))["instructions"]
    assert [item["source"] for item in block["layers"]] == ["platform"]
    assert run["instructionsHash"] == block["hash"]
