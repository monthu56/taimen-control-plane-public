"""Outputs of a task type as verification checks (CP-ADR-0072 §9, CP-ADR-0067 amendment, A006).

A ``deterministic`` check may name an artifact of the task instead of a
skill; it is looked at synchronously, by records only. The required outputs
of a task's type become such checks on every completion, before the task's
own acceptance: a task whose type expects an output is not done without it,
even with no acceptance of its own. Artifact types are named neutrally.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from control_plane.config import Settings
from control_plane.infrastructure.content_store import InMemoryContentStore
from control_plane.worker.main import Worker
from tests.helpers import auth, create_task, do_bootstrap

PLAN_OUTPUT = {"key": "plan", "type": "plan-doc", "required": True}


@pytest.fixture
def store(app: FastAPI) -> InMemoryContentStore:
    content_store = InMemoryContentStore()
    app.state.content_store = content_store
    return content_store


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> str:
    body = await do_bootstrap(client)
    key: str = body["apiKey"]["key"]
    for type_key in ("plan-doc", "notes"):
        response = await client.post(
            "/api/v1/artifact-types",
            json={
                "key": type_key,
                "displayName": type_key,
                "mediaTypes": ["text/*", "application/pdf"],
            },
            headers=auth(key),
        )
        assert response.status_code == 201, response.text
    return key


async def producer_type(client: httpx.AsyncClient, key: str, *outputs: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "producer",
            "displayName": "Producer",
            "artifactSchema": {"outputs": list(outputs)},
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def upload(client: httpx.AsyncClient, key: str, body: bytes, media_type: str) -> str:
    response = await client.put(
        "/api/v1/artifact-contents",
        content=body,
        headers={**auth(key), "Content-Type": media_type},
    )
    assert response.status_code == 201, response.text
    content_ref: str = response.json()["contentRef"]
    return content_ref


async def hand_in(
    client: httpx.AsyncClient,
    key: str,
    task_id: str,
    type_: str,
    *,
    content: bytes | None = None,
    media_type: str = "text/markdown",
    **extra: Any,
) -> dict[str, Any]:
    if content is not None:
        extra["contentRef"] = await upload(client, key, content, media_type)
    response = await client.post(
        "/api/v1/artifacts",
        json={"task": task_id, "type": type_, "name": f"{type_}.out", **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def get_task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def complete(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    current = await get_task(client, key, ref)
    response = await client.post(
        f"/api/v1/tasks/{ref}:complete",
        headers={**auth(key), "If-Match": f'"task-{current["version"]}"'},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def latest_attempt(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}/verifications", headers=auth(key))
    assert response.status_code == 200, response.text
    attempt: dict[str, Any] = response.json()["items"][0]
    return attempt


async def verify(
    client: httpx.AsyncClient, worker: Worker, key: str, ref: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Hand the task in and let one worker pass run its checks."""
    await complete(client, key, ref)
    await worker.run_once()
    return await get_task(client, key, ref), await latest_attempt(client, key, ref)


# --- a required output --------------------------------------------------------


async def test_a_required_output_is_checked_even_without_acceptance(
    client: httpx.AsyncClient, worker: Worker, admin: str, store: InMemoryContentStore
) -> None:
    await producer_type(client, admin, {**PLAN_OUTPUT, "mediaTypes": ["text/markdown"]})
    task = await create_task(client, admin, title="Write the plan", typeKey="producer")
    assert task["acceptance"] == []

    handed_in = await complete(client, admin, task["id"])
    assert handed_in["systemStatusCategory"] != "terminal_success"
    assert handed_in["verification"]["status"] == "running"

    await worker.run_once()
    failed = await get_task(client, admin, task["id"])
    assert failed["systemStatusCategory"] != "terminal_success"
    attempt = await latest_attempt(client, admin, task["id"])
    assert attempt["status"] == "failed"
    assert attempt["checks"] == [
        {
            "key": "output.plan",
            "kind": "deterministic",
            "description": "Required output plan (plan-doc)",
            "spec": {
                "artifact": {
                    "type": "plan-doc",
                    "mediaTypes": ["text/markdown"],
                    "content": "required",
                }
            },
        }
    ]
    assert attempt["results"][0]["reason"] == "artifact_missing"

    plan = await hand_in(client, admin, task["id"], "plan-doc", content=b"# plan")
    done, attempt = await verify(client, worker, admin, task["id"])
    assert attempt["status"] == "passed"
    assert attempt["results"][0]["evidence"] == [{"kind": "artifact", "ref": plan["id"]}]
    assert done["systemStatusCategory"] == "terminal_success"


async def test_outputs_run_before_the_acceptance_of_the_task(
    client: httpx.AsyncClient, worker: Worker, admin: str, store: InMemoryContentStore
) -> None:
    await producer_type(client, admin, PLAN_OUTPUT)
    own = {"key": "looked-at", "kind": "external_state", "description": "Someone looked"}
    task = await create_task(
        client, admin, title="Plan and review", typeKey="producer", acceptance=[own]
    )

    _, attempt = await verify(client, worker, admin, task["id"])
    assert [check["key"] for check in attempt["checks"]] == ["output.plan", "looked-at"]
    # The output failed first: the evidence check never ran.
    assert attempt["status"] == "failed"
    assert [r["key"] for r in attempt["results"]] == ["output.plan"]
    assert attempt["results"][0]["reason"] == "artifact_missing"

    await hand_in(client, admin, task["id"], "plan-doc", content=b"# plan")
    _, attempt = await verify(client, worker, admin, task["id"])
    # The output passed; the task's own check now waits for its evidence.
    assert attempt["status"] == "waiting_external"
    assert attempt["cursor"] == 1


async def test_media_type_and_content_are_checked_on_head_revisions(
    client: httpx.AsyncClient, worker: Worker, admin: str, store: InMemoryContentStore
) -> None:
    await producer_type(client, admin, {**PLAN_OUTPUT, "mediaTypes": ["text/markdown"]})
    task = await create_task(client, admin, title="Write the plan", typeKey="producer")

    # A reference has no media type: it fits no declared one.
    await hand_in(client, admin, task["id"], "plan-doc", uri="https://example.test/plan")
    await hand_in(
        client, admin, task["id"], "plan-doc", content=b"%PDF", media_type="application/pdf"
    )
    _, attempt = await verify(client, worker, admin, task["id"])
    assert attempt["results"][0]["reason"] == "artifact_media_type"

    first = await hand_in(client, admin, task["id"], "plan-doc", content=b"# v1")
    purged = await client.post(
        f"/api/v1/artifacts/{first['id']}:purge-content",
        json={"reason": "withdrawn"},
        headers=auth(admin),
    )
    assert purged.status_code == 200, purged.text
    _, attempt = await verify(client, worker, admin, task["id"])
    assert attempt["results"][0]["reason"] == "artifact_content_missing"

    second = await hand_in(
        client, admin, task["id"], "plan-doc", content=b"# v2", supersedesArtifactId=first["id"]
    )
    done, attempt = await verify(client, worker, admin, task["id"])
    assert attempt["status"] == "passed"
    assert attempt["results"][0]["evidence"] == [{"kind": "artifact", "ref": second["id"]}]
    assert done["systemStatusCategory"] == "terminal_success"


async def test_a_superseded_revision_does_not_count(
    client: httpx.AsyncClient, worker: Worker, admin: str, store: InMemoryContentStore
) -> None:
    await producer_type(client, admin, PLAN_OUTPUT)
    task = await create_task(client, admin, title="Write the plan", typeKey="producer")
    old = await hand_in(client, admin, task["id"], "plan-doc", content=b"# v0")
    await hand_in(
        client,
        admin,
        task["id"],
        "plan-doc",
        uri="https://example.test/v1",
        supersedesArtifactId=old["id"],
    )
    _, attempt = await verify(client, worker, admin, task["id"])
    assert attempt["results"][0]["reason"] == "artifact_content_missing"


async def test_an_optional_output_is_no_check_and_content_may_be_optional(
    client: httpx.AsyncClient, worker: Worker, admin: str
) -> None:
    await producer_type(
        client,
        admin,
        {**PLAN_OUTPUT, "content": "optional"},
        {"key": "notes", "type": "notes"},
    )
    task = await create_task(client, admin, title="Link the plan", typeKey="producer")
    await hand_in(client, admin, task["id"], "plan-doc", uri="https://example.test/plan")

    done, attempt = await verify(client, worker, admin, task["id"])
    assert [check["key"] for check in attempt["checks"]] == ["output.plan"]
    assert attempt["status"] == "passed"
    assert done["systemStatusCategory"] == "terminal_success"


async def test_a_type_without_outputs_completes_as_before(
    client: httpx.AsyncClient, admin: str
) -> None:
    task = await create_task(client, admin, title="Plain")
    done = await complete(client, admin, task["id"])
    assert done["systemStatusCategory"] == "terminal_success"
    assert done["verification"] is None


# --- an artifact check in the acceptance ---------------------------------------


async def test_an_acceptance_check_may_name_an_artifact_of_the_task(
    client: httpx.AsyncClient, worker: Worker, admin: str
) -> None:
    check = {
        "key": "notes-handed-in",
        "kind": "deterministic",
        "description": "Notes are attached",
        "spec": {"artifact": {"type": "notes", "content": "optional"}},
    }
    task = await create_task(client, admin, title="Take notes", acceptance=[check])
    _, attempt = await verify(client, worker, admin, task["id"])
    assert attempt["results"][0]["reason"] == "artifact_missing"

    notes = await hand_in(client, admin, task["id"], "notes", uri="https://example.test/n")
    done, attempt = await verify(client, worker, admin, task["id"])
    assert attempt["results"][0]["evidence"] == [{"kind": "artifact", "ref": notes["id"]}]
    assert done["systemStatusCategory"] == "terminal_success"


@pytest.mark.parametrize(
    ("check", "code", "field"),
    [
        (
            {"key": "output.plan", "kind": "external_state", "description": "Reserved"},
            "invalid_acceptance",
            "acceptance[0].key",
        ),
        (
            {
                "key": "unknown-type",
                "kind": "deterministic",
                "description": "Not registered",
                "spec": {"artifact": {"type": "never-registered"}},
            },
            "invalid_acceptance_spec",
            "acceptance[0].spec.artifact.type",
        ),
        (
            {
                "key": "both",
                "kind": "deterministic",
                "description": "Skill and artifact",
                "spec": {"artifact": {"type": "notes"}, "skill": "check.sample@1"},
            },
            "invalid_acceptance_spec",
            "acceptance[0].spec",
        ),
    ],
)
async def test_an_artifact_check_outside_the_grammar_is_refused(
    client: httpx.AsyncClient, admin: str, check: dict[str, Any], code: str, field: str
) -> None:
    response = await client.post(
        "/api/v1/tasks", json={"title": "Refused", "acceptance": [check]}, headers=auth(admin)
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["details"]["field"] == field
