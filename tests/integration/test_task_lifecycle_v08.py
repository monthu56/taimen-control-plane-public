"""Configurable work item lifecycle (ADR-0048).

Two guarantees, and they are separate on purpose:

* a task moves only along an edge its own type declared, and a refused move
  changes NOTHING — not the status, not the version;
* every core decision (claimable, ready, completable) reads the system
  CATEGORY. The tests below prove that by giving a type status keys core has
  never seen, and one deliberately misleading key: ``done``, category
  ``active``.
"""

from typing import Any

import httpx
import pytest

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    auth,
    claim_task,
    create_agent_with_key,
    do_bootstrap,
    open_session,
)
from tests.integration.test_task_types_v08 import QUESTION_LIFECYCLE

# A type whose keys deliberately lie about their meaning: "done" is ACTIVE and
# "archived" is a successful terminal. Nothing in core may read the key.
MISLEADING_LIFECYCLE: dict[str, Any] = {
    "initialStatus": "done",
    "statuses": [
        {"key": "done", "displayName": "Not actually done", "category": "active"},
        {"key": "archived", "displayName": "Actually finished", "category": "terminal_success"},
        {"key": "void", "displayName": "Void", "category": "terminal_cancelled"},
    ],
    "transitions": [{"from": "done", "to": ["archived", "void"]}],
    "completionStatus": "archived",
}

# No claim/release statuses and no edge out of the initial status other than
# completion: used to pin the "undeclared automatic transition is skipped, not
# fatal" rule.
BARE_LIFECYCLE: dict[str, Any] = {
    "initialStatus": "open",
    "statuses": [
        {"key": "open", "category": "active"},
        {"key": "closed", "category": "terminal_success"},
    ],
    "transitions": [{"from": "open", "to": ["closed"]}],
    "claimStatus": "open",
}


def if_match(key: str, version: int) -> dict[str, str]:
    return {**auth(key), "If-Match": f'"task-{version}"'}


async def make_type(client: httpx.AsyncClient, key: str, name: str, lifecycle: Any) -> str:
    response = await client.post(
        "/api/v1/task-types",
        json={"key": name, "displayName": name, "lifecycleSchema": lifecycle},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def make_task(client: httpx.AsyncClient, key: str, type_key: str, **extra: Any) -> Any:
    response = await client.post(
        "/api/v1/tasks",
        json={"title": f"{type_key} task", "typeKey": type_key, **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- creation -----------------------------------------------------------------


async def test_task_carries_its_type_and_the_pair_of_statuses(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)

    task = await make_task(client, admin_key, "question")

    assert task["typeKey"] == "question"
    assert task["typeVersion"] == 1
    assert task["status"] == "asked"
    assert task["systemStatusCategory"] == "backlog"


async def test_default_type_and_status_reproduce_pre_v08_behaviour(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    task = (
        await client.post("/api/v1/tasks", json={"title": "Plain"}, headers=auth(admin_key))
    ).json()

    assert (task["typeKey"], task["status"], task["systemStatusCategory"]) == (
        "task",
        "todo",
        "active",
    )


async def test_creation_status_must_be_initial_or_backlog(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)

    # `investigating` is active but not the initial status: creating a task
    # already in progress would be a claim-less lie about state.
    refused = await client.post(
        "/api/v1/tasks",
        json={"title": "Q", "typeKey": "question", "status": "investigating"},
        headers=auth(admin_key),
    )
    unknown = await client.post(
        "/api/v1/tasks",
        json={"title": "Q", "typeKey": "question", "status": "in_progress"},
        headers=auth(admin_key),
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_status"
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "status_not_in_lifecycle"


# --- transitions --------------------------------------------------------------


async def test_declared_transition_moves_both_halves_of_the_pair(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question")

    moved = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "investigating"},
        headers=if_match(admin_key, task["version"]),
    )

    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "investigating"
    assert moved.json()["systemStatusCategory"] == "active"
    assert moved.json()["version"] == task["version"] + 1


async def test_undeclared_transition_changes_neither_status_nor_version(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question")

    # asked -> waiting is not declared.
    refused = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "waiting"},
        headers=if_match(admin_key, task["version"]),
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_transition"
    assert refused.json()["error"]["details"]["allowed"] == ["dropped", "investigating"]
    after = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert (after["status"], after["version"]) == (task["status"], task["version"])


async def test_status_outside_the_lifecycle_is_rejected(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question")

    refused = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "in_progress"},
        headers=if_match(admin_key, task["version"]),
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "status_not_in_lifecycle"
    assert "asked" in refused.json()["error"]["details"]["known"]


async def test_transition_to_the_same_status_is_refused(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = (
        await client.post("/api/v1/tasks", json={"title": "Plain"}, headers=auth(admin_key))
    ).json()

    refused = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "todo"},
        headers=if_match(admin_key, task["version"]),
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_transition"


async def test_patch_cannot_reach_terminal_success(client: httpx.AsyncClient) -> None:
    """Completion does more than write a status, so it stays a separate action."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "misleading", MISLEADING_LIFECYCLE)
    task = await make_task(client, admin_key, "misleading")

    refused = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "archived"},
        headers=if_match(admin_key, task["version"]),
    )

    assert refused.status_code == 422
    assert "complete" in refused.json()["error"]["message"]


async def test_terminal_cancelled_stays_reachable_by_patch(client: httpx.AsyncClient) -> None:
    """`cancelled` was patchable before v0.8 and remains so."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = (
        await client.post("/api/v1/tasks", json={"title": "Plain"}, headers=auth(admin_key))
    ).json()

    cancelled = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "cancelled"},
        headers=if_match(admin_key, task["version"]),
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["systemStatusCategory"] == "terminal_cancelled"


# --- core decisions read the category -----------------------------------------


async def test_core_decisions_follow_the_category_not_the_key(
    client: httpx.AsyncClient,
) -> None:
    """A task sitting in a status literally called "done" is claimable."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "misleading", MISLEADING_LIFECYCLE)
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = await make_task(client, admin_key, "misleading")
    assert task["status"] == "done"

    claimed = await claim_task(client, agent_key, task["id"], session["id"])

    assert claimed.status_code == 200, claimed.text


async def test_claiming_a_terminal_task_is_refused_whatever_the_key(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "misleading", MISLEADING_LIFECYCLE)
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = await make_task(client, admin_key, "misleading")
    cancelled = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "void"},
        headers=if_match(admin_key, task["version"]),
    )
    assert cancelled.status_code == 200

    refused = await claim_task(client, agent_key, task["id"], session["id"])

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "task_not_claimable"


async def test_blocked_category_remains_claimable(client: httpx.AsyncClient) -> None:
    """Pre-v0.8 behaviour, preserved deliberately (SPEC §6.1)."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = (
        await client.post("/api/v1/tasks", json={"title": "Plain"}, headers=auth(admin_key))
    ).json()
    blocked = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "blocked"},
        headers=if_match(admin_key, task["version"]),
    )
    assert blocked.json()["systemStatusCategory"] == "blocked"

    claimed = await claim_task(client, agent_key, task["id"], session["id"])

    assert claimed.status_code == 200, claimed.text


# --- claim, release, completion ------------------------------------------------


async def test_claim_and_release_walk_the_declared_service_statuses(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = await make_task(client, admin_key, "question")

    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    claimed = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    await client.post(f"/api/v1/claims/{claim['id']}:release", json={}, headers=auth(agent_key))
    released = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()

    assert (claimed["status"], claimed["systemStatusCategory"]) == ("investigating", "active")
    assert (released["status"], released["systemStatusCategory"]) == ("asked", "backlog")


async def test_claim_succeeds_when_the_claim_edge_is_undeclared(
    client: httpx.AsyncClient,
) -> None:
    """Coordination must not depend on a cosmetic gap in configuration."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "bare", BARE_LIFECYCLE)
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = await make_task(client, admin_key, "bare")

    claimed = await claim_task(client, agent_key, task["id"], session["id"])

    assert claimed.status_code == 200, claimed.text
    after = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert after["status"] == "open"  # `open -> open` is not an edge; nothing moved


async def test_completion_writes_the_types_completion_status(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question")
    moved = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "investigating"},
        headers=if_match(admin_key, task["version"]),
    )

    completed = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={},
        headers=if_match(admin_key, moved.json()["version"]),
    )

    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "answered"
    assert completed.json()["systemStatusCategory"] == "terminal_success"
    assert completed.json()["completedAt"] is not None


async def test_completion_along_an_undeclared_edge_is_refused(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question")  # `asked`; no edge to `answered`

    refused = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={},
        headers=if_match(admin_key, task["version"]),
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_transition"
    after = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert (after["status"], after["version"]) == (task["status"], task["version"])


@pytest.mark.parametrize(
    ("terminal_status", "expected_status", "expected_code"),
    [("archived", 409, "task_already_completed"), ("void", 422, "task_cancelled")],
)
async def test_completing_a_terminal_task_reports_by_category(
    client: httpx.AsyncClient, terminal_status: str, expected_status: int, expected_code: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "misleading", MISLEADING_LIFECYCLE)
    task = await make_task(client, admin_key, "misleading")
    if terminal_status == "archived":
        finished = await client.post(
            f"/api/v1/tasks/{task['id']}:complete",
            json={},
            headers=if_match(admin_key, task["version"]),
        )
    else:
        finished = await client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"status": "void"},
            headers=if_match(admin_key, task["version"]),
        )
    assert finished.status_code == 200, finished.text

    again = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={},
        headers=if_match(admin_key, finished.json()["version"]),
    )

    assert again.status_code == expected_status
    assert again.json()["error"]["code"] == expected_code


# --- dependency readiness ------------------------------------------------------


async def test_prerequisite_is_satisfied_by_the_success_category(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "misleading", MISLEADING_LIFECYCLE)
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    dependent = await make_task(client, admin_key, "misleading")
    prerequisite = await make_task(client, admin_key, "misleading")
    await client.post(
        f"/api/v1/tasks/{dependent['id']}/relations",
        json={"toTask": prerequisite["id"], "type": "depends_on"},
        headers=auth(admin_key),
    )

    # The prerequisite SITS in a status called "done" — and still blocks,
    # because its category is `active`.
    blocked = await claim_task(client, agent_key, dependent["id"], session["id"])
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "task_not_ready"

    await client.post(
        f"/api/v1/tasks/{prerequisite['id']}:complete",
        json={},
        headers=if_match(admin_key, prerequisite["version"]),
    )
    ready = await claim_task(client, agent_key, dependent["id"], session["id"])

    assert ready.status_code == 200, ready.text


async def test_cancelled_prerequisite_keeps_blocking(client: httpx.AsyncClient) -> None:
    """Documented pre-v0.8 behaviour, now expressed by category."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    dependent = (
        await client.post("/api/v1/tasks", json={"title": "Dependent"}, headers=auth(admin_key))
    ).json()
    prerequisite = (
        await client.post("/api/v1/tasks", json={"title": "Prereq"}, headers=auth(admin_key))
    ).json()
    await client.post(
        f"/api/v1/tasks/{dependent['id']}/relations",
        json={"toTask": prerequisite["id"], "type": "depends_on"},
        headers=auth(admin_key),
    )
    await client.patch(
        f"/api/v1/tasks/{prerequisite['id']}",
        json={"status": "cancelled"},
        headers=if_match(admin_key, prerequisite["version"]),
    )

    blocked = await claim_task(client, agent_key, dependent["id"], session["id"])

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "task_not_ready"


# --- listing -------------------------------------------------------------------


async def test_tasks_can_be_filtered_by_category_and_type(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    await make_task(client, admin_key, "question")
    await client.post("/api/v1/tasks", json={"title": "Plain"}, headers=auth(admin_key))

    by_category = await client.get(
        "/api/v1/tasks?systemStatusCategory=backlog", headers=auth(admin_key)
    )
    by_type = await client.get("/api/v1/tasks?typeKey=question", headers=auth(admin_key))
    unknown_key = await client.get("/api/v1/tasks?status=review", headers=auth(admin_key))
    unknown_category = await client.get(
        "/api/v1/tasks?systemStatusCategory=paused", headers=auth(admin_key)
    )

    assert [t["typeKey"] for t in by_category.json()["items"]] == ["question"]
    assert [t["typeKey"] for t in by_type.json()["items"]] == ["question"]
    # An unknown KEY is an empty page (no global vocabulary any more)...
    assert unknown_key.status_code == 200 and unknown_key.json()["items"] == []
    # ...but the CATEGORY vocabulary is global, so a wrong one is a 422.
    assert unknown_category.status_code == 422
    assert unknown_category.json()["error"]["code"] == "invalid_status_category"


async def test_client_cannot_set_the_category(client: httpx.AsyncClient) -> None:
    """The pair is derived; a client that could set the category could lie."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await client.post(
        "/api/v1/tasks",
        json={"title": "Plain", "systemStatusCategory": "terminal_success"},
        headers=auth(admin_key),
    )

    assert response.status_code == 400, response.text


# --- the transition projection ------------------------------------------------


async def test_transitions_report_the_declared_edges_of_the_type(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question")

    body = (
        await client.get(f"/api/v1/tasks/{task['id']}/transitions", headers=auth(admin_key))
    ).json()

    assert (body["typeKey"], body["typeVersion"]) == ("question", 1)
    assert (body["status"], body["systemStatusCategory"]) == ("asked", "backlog")
    assert body["targets"] == [
        {
            "status": "dropped",
            "displayName": "Dropped",
            "systemStatusCategory": "terminal_cancelled",
            "route": "update",
        },
        {
            "status": "investigating",
            "displayName": "Investigating",
            "systemStatusCategory": "active",
            "route": "update",
        },
    ]


async def test_transitions_agree_with_what_the_write_paths_accept(
    client: httpx.AsyncClient,
) -> None:
    """The projection is only useful if it predicts the 422s exactly."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question", status="asked")
    moved = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "investigating"},
        headers=if_match(admin_key, task["version"]),
    )
    version = moved.json()["version"]

    body = (
        await client.get(f"/api/v1/tasks/{task['id']}/transitions", headers=auth(admin_key))
    ).json()
    routes = {t["status"]: t["route"] for t in body["targets"]}

    assert routes == {
        "asked": "update",
        "waiting": "update",
        "dropped": "update",
        "answered": "complete",
    }
    # A 'complete' target is refused by PATCH...
    refused = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "answered"},
        headers=if_match(admin_key, version),
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_status"
    # ...and an 'update' target is accepted by it.
    accepted = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "waiting"},
        headers=if_match(admin_key, version),
    )
    assert accepted.status_code == 200, accepted.text


async def test_a_finished_task_offers_no_transitions(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = (
        await client.post("/api/v1/tasks", json={"title": "Plain"}, headers=auth(admin_key))
    ).json()
    await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "cancelled"},
        headers=if_match(admin_key, task["version"]),
    )

    body = (
        await client.get(f"/api/v1/tasks/{task['id']}/transitions", headers=auth(admin_key))
    ).json()

    assert (body["status"], body["targets"]) == ("cancelled", [])


async def test_reading_transitions_does_not_require_the_registry_permission(
    client: httpx.AsyncClient,
) -> None:
    """An agent may need to move work without the right to reshape the process."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await make_type(client, admin_key, "question", QUESTION_LIFECYCLE)
    task = await make_task(client, admin_key, "question")
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)

    seen = await client.get(f"/api/v1/tasks/{task['id']}/transitions", headers=auth(agent_key))
    registry = await client.get("/api/v1/task-types", headers=auth(agent_key))

    assert seen.status_code == 200, seen.text
    assert [t["status"] for t in seen.json()["targets"]] == ["dropped", "investigating"]
    assert registry.status_code == 403
