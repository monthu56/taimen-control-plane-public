import httpx

from tests.helpers import auth, create_task, do_bootstrap


async def test_create_task_and_public_id_sequence(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    first = await create_task(client, admin_key, title="First")
    second = await create_task(client, admin_key, title="Second")

    assert first["publicId"] == "TASK-000001"
    assert second["publicId"] == "TASK-000002"
    assert first["version"] == 1
    assert first["claimEpoch"] == 0
    assert first["status"] == "todo"
    assert first["createdBy"] == body["adminPrincipal"]["id"]


async def test_get_by_id_and_public_id_with_etag(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    task = await create_task(client, admin_key)

    by_id = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))
    by_public = await client.get(f"/api/v1/tasks/{task['publicId']}", headers=auth(admin_key))
    assert by_id.status_code == by_public.status_code == 200
    assert by_id.json()["id"] == by_public.json()["id"]
    assert by_id.headers["etag"] == '"task-1"'

    missing = await client.get("/api/v1/tasks/TASK-999999", headers=auth(admin_key))
    assert missing.status_code == 404


async def test_patch_requires_if_match(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    task = await create_task(client, admin_key)

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}", json={"title": "New"}, headers=auth(admin_key)
    )
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "if_match_required"

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "New"},
        headers={**auth(admin_key), "If-Match": "garbage"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_if_match"


async def test_patch_updates_and_bumps_version(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    task = await create_task(client, admin_key)

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Renamed", "priority": "high", "status": "blocked"},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["title"] == "Renamed"
    assert updated["priority"] == "high"
    assert updated["status"] == "blocked"
    assert updated["version"] == 2


async def test_version_conflict(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    task = await create_task(client, admin_key)

    # Two clients read version 1; the first update wins.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "First writer"},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 200

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Second writer"},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "version_conflict"
    assert error["details"]["currentVersion"] == 2

    # State kept the first writer's change.
    current = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))
    assert current.json()["title"] == "First writer"


async def test_done_only_via_complete(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    task = await create_task(client, admin_key)

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "done"},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 422

    response = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 200
    completed = response.json()
    assert completed["status"] == "done"
    assert completed["completedAt"] is not None

    # Completing again conflicts.
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        headers={**auth(admin_key), "If-Match": f'"task-{completed["version"]}"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "task_already_completed"


async def test_task_validation(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    response = await client.post(
        "/api/v1/tasks", json={"title": "x", "priority": "urgent"}, headers=auth(admin_key)
    )
    assert response.status_code == 422
    response = await client.post(
        "/api/v1/tasks", json={"title": "x", "status": "in_progress"}, headers=auth(admin_key)
    )
    assert response.status_code == 422
    response = await client.post("/api/v1/tasks", json={}, headers=auth(admin_key))
    assert response.status_code == 400  # contract violation: missing title


async def test_task_list_pagination_and_filters(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    for i in range(5):
        await create_task(client, admin_key, title=f"Task {i}")

    response = await client.get("/api/v1/tasks?limit=2", headers=auth(admin_key))
    assert response.status_code == 200
    page1 = response.json()
    assert len(page1["items"]) == 2
    assert page1["nextCursor"]

    response = await client.get(
        f"/api/v1/tasks?limit=2&cursor={page1['nextCursor']}", headers=auth(admin_key)
    )
    page2 = response.json()
    assert len(page2["items"]) == 2

    response = await client.get(
        f"/api/v1/tasks?limit=2&cursor={page2['nextCursor']}", headers=auth(admin_key)
    )
    page3 = response.json()
    assert len(page3["items"]) == 1
    assert page3["nextCursor"] is None

    ids = [t["id"] for t in page1["items"] + page2["items"] + page3["items"]]
    assert len(set(ids)) == 5

    response = await client.get("/api/v1/tasks?limit=500", headers=auth(admin_key))
    assert response.status_code == 422

    response = await client.get("/api/v1/tasks?status=done", headers=auth(admin_key))
    assert response.json()["items"] == []
