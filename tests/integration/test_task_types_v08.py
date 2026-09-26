"""Work item type registry (ADR-0048).

The guarantee under test is the one inherited from ADR-0030: a type version is
allocated by the server and is immutable from the moment it is written, so a
tenant can reshape its process without changing the semantics of work already
in flight. What is new here is that the registry also owns the tenant's status
vocabulary, which is why it needs a permission of its own.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.helpers import auth, create_agent_with_key, do_bootstrap

# A question is the case ADR-0015 opened with: states core has never heard of,
# mapped onto the five system categories.
QUESTION_LIFECYCLE: dict[str, Any] = {
    "initialStatus": "asked",
    "statuses": [
        {"key": "asked", "displayName": "Asked", "category": "backlog"},
        {"key": "investigating", "displayName": "Investigating", "category": "active"},
        {"key": "waiting", "displayName": "Waiting for input", "category": "blocked"},
        {"key": "answered", "displayName": "Answered", "category": "terminal_success"},
        {"key": "dropped", "displayName": "Dropped", "category": "terminal_cancelled"},
    ],
    "transitions": [
        {"from": "asked", "to": ["investigating", "dropped"]},
        # `investigating -> asked` is what a released claim walks back along.
        {"from": "investigating", "to": ["asked", "waiting", "answered", "dropped"]},
        {"from": "waiting", "to": ["investigating", "dropped"]},
    ],
    "claimStatus": "investigating",
    "releaseStatus": "asked",
    "completionStatus": "answered",
}


async def create_type(
    client: httpx.AsyncClient, key: str, *, type_key: str = "question", **extra: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/task-types",
        json={
            "key": type_key,
            "displayName": type_key.title(),
            "lifecycleSchema": QUESTION_LIFECYCLE,
            **extra,
        },
        headers=auth(key),
    )


async def test_bootstrap_creates_the_system_type(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    page = (await client.get("/api/v1/task-types", headers=auth(admin_key))).json()

    assert [(t["key"], t["version"], t["status"]) for t in page["items"]] == [("task", 1, "active")]
    lifecycle = page["items"][0]["lifecycleSchema"]
    assert lifecycle["initialStatus"] == "todo"
    assert lifecycle["completionStatus"] == "done"


async def test_versions_are_allocated_by_the_server(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    first = await create_type(client, admin_key)
    second = await create_type(client, admin_key)

    assert first.status_code == 201 and second.status_code == 201
    assert (first.json()["version"], second.json()["version"]) == (1, 2)
    assert first.json()["id"] != second.json()["id"]


async def test_client_cannot_supply_a_version(client: httpx.AsyncClient) -> None:
    """Version allocation is the server's; the request schema has no room for it."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    created = await create_type(client, admin_key, version=99)

    assert created.status_code == 400, created.text
    assert (await client.get("/api/v1/task-types?key=question", headers=auth(admin_key))).json()[
        "items"
    ] == []


@pytest.mark.parametrize(
    "lifecycle",
    [
        pytest.param({"initialStatus": "x", "statuses": []}, id="empty"),
        pytest.param(
            {
                "initialStatus": "open",
                "statuses": [{"key": "open", "category": "paused"}],
            },
            id="project-category",
        ),
        pytest.param(
            {
                "initialStatus": "open",
                "statuses": [{"key": "open", "category": "active"}],
            },
            id="no-success-status",
        ),
        pytest.param(
            {
                "initialStatus": "open",
                "statuses": [
                    {"key": "open", "category": "active"},
                    {"key": "done", "category": "terminal_success"},
                ],
                "claimStatus": "done",
            },
            id="terminal-claim-status",
        ),
    ],
)
async def test_invalid_lifecycle_is_refused_and_writes_nothing(
    client: httpx.AsyncClient, lifecycle: dict[str, Any]
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await create_type(client, admin_key, lifecycleSchema=lifecycle)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_lifecycle_schema"
    page = (await client.get("/api/v1/task-types?key=question", headers=auth(admin_key))).json()
    assert page["items"] == []


async def test_remote_ref_in_field_schema_is_refused(client: httpx.AsyncClient) -> None:
    """A stored schema must not be able to reach out of the process."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await create_type(
        client, admin_key, fieldSchema={"$ref": "https://example.invalid/schema.json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_json_schema"


async def test_version_content_is_immutable_even_against_raw_sql(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await create_type(client, admin_key)).json()

    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_types SET lifecycle_schema = '{}'::jsonb WHERE id = :id"),
            {"id": created["id"]},
        )
    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM task_types WHERE id = :id"), {"id": created["id"]})


async def test_deprecate_is_idempotent_and_one_way(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await create_type(client, admin_key)).json()

    first = await client.post(
        f"/api/v1/task-types/{created['id']}:deprecate", headers=auth(admin_key)
    )
    second = await client.post(
        f"/api/v1/task-types/{created['id']}:deprecate", headers=auth(admin_key)
    )

    assert first.status_code == 200 and first.json()["status"] == "deprecated"
    assert second.status_code == 200 and second.json()["status"] == "deprecated"
    with pytest.raises(DBAPIError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_types SET status = 'active' WHERE id = :id"), {"id": created["id"]}
        )


async def test_deprecated_version_stops_resolving_but_keeps_its_tasks_alive(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await create_type(client, admin_key)).json()
    task = (
        await client.post(
            "/api/v1/tasks",
            json={"title": "Why?", "typeKey": "question"},
            headers=auth(admin_key),
        )
    ).json()

    await client.post(f"/api/v1/task-types/{created['id']}:deprecate", headers=auth(admin_key))

    # New work cannot be filed against it any more...
    refused = await client.post(
        "/api/v1/tasks",
        json={"title": "Why again?", "typeKey": "question"},
        headers=auth(admin_key),
    )
    assert refused.status_code == 404
    # ...but the task that already carries it still moves.
    moved = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "investigating"},
        headers={**auth(admin_key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "investigating"


async def test_the_last_active_system_type_version_cannot_be_deprecated(
    client: httpx.AsyncClient,
) -> None:
    """Otherwise `POST /tasks` without a typeKey becomes a 404 for everyone."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    system = (await client.get("/api/v1/task-types?key=task", headers=auth(admin_key))).json()[
        "items"
    ][0]

    response = await client.post(
        f"/api/v1/task-types/{system['id']}:deprecate", headers=auth(admin_key)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "system_task_type_required"


async def test_task_types_require_their_own_permission(client: httpx.AsyncClient) -> None:
    """Filing work does not confer the right to redefine what work IS."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, writer_key = await create_agent_with_key(
        client, admin_key, permissions=["tasks.read", "tasks.write"]
    )

    created = await create_type(client, writer_key)
    listed = await client.get("/api/v1/task-types", headers=auth(writer_key))

    assert created.status_code == 403
    assert listed.status_code == 403

    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["task_types.read"]
    )
    assert (await client.get("/api/v1/task-types", headers=auth(reader_key))).status_code == 200
    assert (await create_type(client, reader_key, type_key="other")).status_code == 403


async def test_task_type_does_not_cross_the_tenant_boundary(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    from tests.helpers import make_tenant_directly

    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = (await create_type(client, admin_key)).json()
    _, other_key = make_tenant_directly(sync_engine, "other")

    assert (
        await client.get(f"/api/v1/task-types/{created['id']}", headers=auth(other_key))
    ).status_code == 404
    visible = (await client.get("/api/v1/task-types", headers=auth(other_key))).json()["items"]
    assert [t["key"] for t in visible] == ["task"]  # only its own system type
    foreign = await client.post(
        "/api/v1/tasks",
        json={"title": "Borrowed type", "typeId": created["id"]},
        headers=auth(other_key),
    )
    assert foreign.status_code == 404
