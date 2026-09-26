"""M1.1 work graph: Goals, and goal/origin/acceptance/evidence on tasks (CP-ADR-0062).

What is under test:

*A Goal is a governed record.* It has its own rights (``goals.read`` /
``goals.write``), optimistic concurrency, a status whose closure time cannot
drift from it, and a hierarchy that refuses cycles.

*Origin is a record, not a claim the client can revise.* It is validated on
write (a rule-born item names its rule and at least one fact), derived by the
core when omitted, and cannot be changed afterwards.

*Evidence points at facts that exist.* Observation and artifact ids are
resolved in this tenant; a foreign or unknown id is a 404, and evidence tied to
an acceptance check must name a check the task declares.

*The journal carries references.* ``goal.*`` and ``task.created`` carry ids,
kinds and counts — not the desired state, not check specs, not notes.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from platform_auth import ObjectPage, PolicyDecision
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.application.commands.goals import create_goal, update_goal
from control_plane.application.commands.tasks import create_task as create_task_command
from control_plane.application.queries.goals import list_goals
from control_plane.domain.errors import NotFoundError
from control_plane.infrastructure.db.models import Goal
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)

BEFORE_WORK_GRAPH = "e3b8c1a6d9f4"
WORK_GRAPH = "d2f7a3c9b1e5"

CRITERIA = [
    {
        "key": "all-accepted-decisions-hold",
        "kind": "deterministic",
        "description": "Every accepted decision has a passing conformance check",
        "spec": {"suite": "conformance"},
    },
    {"key": "owner-signs-off", "kind": "human", "description": "The owner confirms"},
]


async def post_goal(client: httpx.AsyncClient, key: str, **body: Any) -> httpx.Response:
    return await client.post(
        "/api/v1/goals", json={"title": "Core matches its decisions", **body}, headers=auth(key)
    )


async def make_goal(client: httpx.AsyncClient, key: str, **body: Any) -> dict[str, Any]:
    response = await post_goal(client, key, **body)
    assert response.status_code == 201, response.text
    return response.json()


async def patch_goal(
    client: httpx.AsyncClient, key: str, goal: dict[str, Any], **body: Any
) -> httpx.Response:
    return await client.patch(
        f"/api/v1/goals/{goal['id']}",
        json=body,
        headers={**auth(key), "If-Match": f'"goal-{goal["version"]}"'},
    )


async def patch_task(
    client: httpx.AsyncClient, key: str, task: dict[str, Any], **body: Any
) -> httpx.Response:
    return await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json=body,
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )


async def observe(client: httpx.AsyncClient, key: str, content: str = "seen") -> str:
    response = await client.post(
        "/api/v1/observations",
        json={
            "kind": "external_fact",
            "content": content,
            "source": "connector",
            "dedupKey": content,
            "externalRef": {"system": "vcs", "id": content},
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def artifact(client: httpx.AsyncClient, key: str, task_ref: str) -> str:
    response = await client.post(
        "/api/v1/artifacts",
        json={"type": "report", "name": "conformance report", "task": task_ref},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def event_payloads(sync_engine: Engine, event_type: str) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM events WHERE event_type = :t ORDER BY sequence"),
            {"t": event_type},
        ).all()
    return [row[0] for row in rows]


# --- goals --------------------------------------------------------------------


async def test_goal_lifecycle_create_read_update(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]

    goal = await make_goal(
        client,
        admin_key,
        desiredState="Every accepted decision is reflected in the running system",
        criteria=CRITERIA,
        ownerId=admin_id,
    )
    assert goal["status"] == "active"
    assert goal["closedAt"] is None
    assert goal["version"] == 1
    assert goal["criteria"] == CRITERIA
    # Written by a person's credential without a createdFrom: human.
    assert goal["createdFrom"] == {"kind": "human", "evidence": []}

    read = await client.get(f"/api/v1/goals/{goal['id']}", headers=auth(admin_key))
    assert read.status_code == 200
    assert read.headers["etag"] == '"goal-1"'
    assert read.json() == goal

    achieved = await patch_goal(client, admin_key, goal, status="achieved")
    assert achieved.status_code == 200, achieved.text
    assert achieved.json()["status"] == "achieved"
    assert achieved.json()["closedAt"] is not None
    assert achieved.json()["version"] == 2

    # The state stopped being true: the goal reopens and is open again.
    reopened = await patch_goal(client, admin_key, achieved.json(), status="active")
    assert reopened.json()["closedAt"] is None
    assert reopened.json()["version"] == 3

    stale = await patch_goal(client, admin_key, goal, title="Late write")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"

    # Restating the current values is not a change: no version, no event.
    same = await patch_goal(client, admin_key, reopened.json(), status="active")
    assert same.status_code == 200
    assert same.json()["version"] == 3

    for body, code in (
        ({}, "empty_update"),
        ({"title": None}, "invalid_field"),
        ({"status": "done"}, "invalid_request"),
        ({"createdFrom": {"kind": "rule"}}, "invalid_request"),
    ):
        refused = await patch_goal(client, admin_key, reopened.json(), **body)
        assert refused.status_code in (400, 422), refused.text
        assert refused.json()["error"]["code"] == code

    no_if_match = await client.patch(
        f"/api/v1/goals/{goal['id']}", json={"title": "x"}, headers=auth(admin_key)
    )
    assert no_if_match.status_code == 428

    created = event_payloads(sync_engine, "goal.created")
    assert len(created) == 1
    assert created[0]["criteriaCount"] == 2
    assert created[0]["createdFrom"] == {"kind": "human", "evidence": []}
    assert "desiredState" not in created[0]
    assert "criteria" not in created[0]
    updated = event_payloads(sync_engine, "goal.updated")
    assert [(u["fromStatus"], u["status"]) for u in updated] == [
        ("active", "achieved"),
        ("achieved", "active"),
    ]

    # The CHECK holds the invariant even against a direct write.
    with pytest.raises(IntegrityError), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE goals SET status = 'achieved' WHERE id = :id"), {"id": goal["id"]}
        )


async def test_goal_listing_filters_and_pages(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    parent = await make_goal(client, admin_key, title="Parent")
    children = [
        await make_goal(client, admin_key, title=f"Child {i}", parentGoalId=parent["id"])
        for i in range(3)
    ]
    await patch_goal(client, admin_key, children[0], status="abandoned")

    page = await client.get(
        "/api/v1/goals", params={"parentGoalId": parent["id"], "limit": 2}, headers=auth(admin_key)
    )
    assert page.status_code == 200
    first = page.json()
    assert [g["title"] for g in first["items"]] == ["Child 2", "Child 1"]
    rest = await client.get(
        "/api/v1/goals",
        params={"parentGoalId": parent["id"], "limit": 2, "cursor": first["nextCursor"]},
        headers=auth(admin_key),
    )
    assert [g["title"] for g in rest.json()["items"]] == ["Child 0"]

    abandoned = await client.get(
        "/api/v1/goals", params={"status": "abandoned"}, headers=auth(admin_key)
    )
    assert [g["id"] for g in abandoned.json()["items"]] == [children[0]["id"]]
    bad = await client.get("/api/v1/goals", params={"status": "done"}, headers=auth(admin_key))
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_goal_status"


async def test_goal_hierarchy_refuses_cycles(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    root = await make_goal(client, admin_key, title="Root")
    middle = await make_goal(client, admin_key, title="Middle", parentGoalId=root["id"])
    leaf = await make_goal(client, admin_key, title="Leaf", parentGoalId=middle["id"])

    own = await patch_goal(client, admin_key, root, parentGoalId=root["id"])
    assert own.status_code == 422
    assert own.json()["error"]["code"] == "goal_cycle"
    loop = await patch_goal(client, admin_key, root, parentGoalId=leaf["id"])
    assert loop.status_code == 422
    assert loop.json()["error"]["code"] == "goal_cycle"

    detached = await patch_goal(client, admin_key, leaf, parentGoalId=None)
    assert detached.status_code == 200
    assert detached.json()["parentGoalId"] is None


async def test_goal_rights_are_separate_from_task_rights(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    goal = await make_goal(client, admin_key)
    _, task_agent = await create_agent_with_key(client, admin_key, name="tasks-only")
    _, reader = await create_agent_with_key(
        client, admin_key, name="goal-reader", permissions=["goals.read"]
    )

    for response in (
        await client.get(f"/api/v1/goals/{goal['id']}", headers=auth(task_agent)),
        await client.get("/api/v1/goals", headers=auth(task_agent)),
        await post_goal(client, task_agent),
        await post_goal(client, reader),
        await patch_goal(client, reader, goal, title="Mine now"),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "permission_denied"

    assert (
        await client.get(f"/api/v1/goals/{goal['id']}", headers=auth(reader))
    ).status_code == 200
    # Reading the work of a goal is reading tasks: goals.read alone is not enough.
    work = await client.get(f"/api/v1/goals/{goal['id']}/work", headers=auth(reader))
    assert work.status_code == 403


async def test_goals_do_not_cross_tenants(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    goal = await make_goal(client, admin_key)
    _, other_key = make_tenant_directly(sync_engine, "other")

    assert (
        await client.get(f"/api/v1/goals/{goal['id']}", headers=auth(other_key))
    ).status_code == 404
    assert (await client.get("/api/v1/goals", headers=auth(other_key))).json()["items"] == []
    foreign_parent = await post_goal(client, other_key, parentGoalId=goal["id"])
    assert foreign_parent.status_code == 404
    foreign_link = await client.post(
        "/api/v1/tasks", json={"title": "Sneaky", "goalId": goal["id"]}, headers=auth(other_key)
    )
    assert foreign_link.status_code == 404


# --- workspace boundary of goal references (review TASK-000323) -----------------


async def test_parent_goal_stays_in_the_childs_workspace(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ws_a = await create_workspace(client, admin_key, "alpha")
    ws_b = await create_workspace(client, admin_key, "beta")
    in_b = await make_goal(client, admin_key, title="B", workspaceId=ws_b["id"])
    tenant_level = await make_goal(client, admin_key, title="Tenant")
    in_a = await make_goal(client, admin_key, title="A", workspaceId=ws_a["id"])

    foreign = await post_goal(client, admin_key, workspaceId=ws_a["id"], parentGoalId=in_b["id"])
    assert foreign.status_code == 422, foreign.text
    assert foreign.json()["error"]["code"] == "goal_workspace_mismatch"
    moved = await patch_goal(client, admin_key, in_a, parentGoalId=in_b["id"])
    assert moved.status_code == 422, moved.text
    assert moved.json()["error"]["code"] == "goal_workspace_mismatch"
    # A tenant-level goal cannot hang under a workspace goal either.
    upward = await post_goal(client, admin_key, parentGoalId=in_a["id"])
    assert upward.json()["error"]["code"] == "goal_workspace_mismatch"

    # A tenant-level parent serves every workspace.
    child = await make_goal(
        client, admin_key, title="A child", workspaceId=ws_a["id"], parentGoalId=tenant_level["id"]
    )
    assert child["parentGoalId"] == tenant_level["id"]
    relinked = await patch_goal(client, admin_key, in_a, parentGoalId=tenant_level["id"])
    assert relinked.status_code == 200, relinked.text


async def test_unreadable_parent_goal_looks_like_a_missing_one(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    parent = await make_goal(client, admin_key, title="Parent")
    _, writer = await create_agent_with_key(
        client, admin_key, name="goal-writer", permissions=["goals.write"]
    )
    own = await make_goal(client, writer, title="Own")

    def shape(response: httpx.Response) -> tuple[int, str, str]:
        error = response.json()["error"]
        return response.status_code, error["code"], error["message"]

    missing = str(uuid.uuid4())
    for create_or_patch in (
        lambda goal_id: post_goal(client, writer, parentGoalId=goal_id),
        lambda goal_id: patch_goal(client, writer, own, parentGoalId=goal_id),
    ):
        unreadable = await create_or_patch(parent["id"])
        absent = await create_or_patch(missing)
        assert shape(unreadable) == shape(absent) == (404, "not_found", "Goal not found")


async def test_task_links_only_to_a_readable_goal_of_its_workspace(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ws_a = await create_workspace(client, admin_key, "alpha")
    ws_b = await create_workspace(client, admin_key, "beta")
    in_a = await make_goal(client, admin_key, title="A", workspaceId=ws_a["id"])
    in_b = await make_goal(client, admin_key, title="B", workspaceId=ws_b["id"])
    tenant_level = await make_goal(client, admin_key, title="Tenant")

    def refused_as_missing(response: httpx.Response) -> bool:
        error = response.json()["error"]
        return (response.status_code, error["code"], error["message"]) == (
            404,
            "not_found",
            "Goal not found",
        )

    # Another workspace's goal, from a task in a workspace or in none: not found.
    for body in ({"workspaceId": ws_a["id"]}, {}):
        foreign = await client.post(
            "/api/v1/tasks",
            json={"title": "Sneaky", "goalId": in_b["id"], **body},
            headers=auth(admin_key),
        )
        assert refused_as_missing(foreign), foreign.text
    task = await create_task(client, admin_key, title="In A", workspaceId=ws_a["id"])
    relink = await patch_task(client, admin_key, task, goalId=in_b["id"])
    assert refused_as_missing(relink), relink.text

    # Its own workspace's goal and a tenant-level goal are both fine.
    linked = await patch_task(client, admin_key, task, goalId=in_a["id"])
    assert linked.status_code == 200, linked.text
    task = linked.json()
    tenant_work = await create_task(
        client, admin_key, title="Tenant work", workspaceId=ws_b["id"], goalId=tenant_level["id"]
    )
    assert tenant_work["goalId"] == tenant_level["id"]

    # Moving the task out of its goal's workspace needs a relink in the same write.
    stranded = await patch_task(client, admin_key, task, workspaceId=ws_b["id"])
    assert stranded.status_code == 422, stranded.text
    assert stranded.json()["error"]["code"] == "goal_workspace_mismatch"
    moved = await patch_task(client, admin_key, task, workspaceId=ws_b["id"], goalId=in_b["id"])
    assert moved.status_code == 200, moved.text
    assert (moved.json()["workspaceId"], moved.json()["goalId"]) == (ws_b["id"], in_b["id"])

    # Without goals.read the goal is as good as missing.
    _, task_writer = await create_agent_with_key(client, admin_key, name="tasks-only")
    blind = await client.post(
        "/api/v1/tasks",
        json={"title": "Blind", "goalId": tenant_level["id"]},
        headers=auth(task_writer),
    )
    assert refused_as_missing(blind), blind.text


@dataclass
class WorkspacePolicy:
    """A PDP that grants everything except ``goals.read`` on the listed workspaces."""

    unreadable: set[str]
    readable: set[str]

    async def check(
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):  # type: ignore[no-untyped-def]
        allowed = not (action == "goals.read" and resource.key in self.unreadable)
        return PolicyDecision(
            allowed=allowed,
            reason_code="allowed" if allowed else "denied_no_binding",
            decision_id=str(uuid.uuid4()),
            policy_version="1",
            model_version="1",
            source="online",
            consistency_token=None,
            evaluated_at=datetime.now(UTC),
            action=action,
            resource=resource.key,
        )

    async def list_objects(self, ctx, action, resource_type, **kwargs):  # type: ignore[no-untyped-def]
        return ObjectPage(objects=sorted(self.readable), cursor=None, model_version="1")


@pytest.fixture
def restore_authorizer() -> Iterator[None]:
    yield
    configure_authorizer(Authorizer(None, "local"))


async def test_policy_mode_decides_goal_references_on_the_goals_workspace(
    client: httpx.AsyncClient, app: Any, restore_authorizer: None
) -> None:
    """A writer in workspace A who may not read goals in B cannot tell a goal
    of B from a missing one — as a parent, as a task's goal, or in a list."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    ws_a = await create_workspace(client, admin_key, "alpha")
    ws_b = await create_workspace(client, admin_key, "beta")
    in_a = await make_goal(client, admin_key, title="A", workspaceId=ws_a["id"])
    in_b = await make_goal(client, admin_key, title="B", workspaceId=ws_b["id"])
    tenant_level = await make_goal(client, admin_key, title="Tenant")

    configure_authorizer(
        Authorizer(
            WorkspacePolicy(unreadable={f"workspace:{ws_b['id']}"}, readable={ws_a["id"]}),
            "policy",
        )
    )
    ctx = AuthContext(
        tenant_id=uuid.UUID(boot["tenant"]["id"]),
        principal_id=uuid.UUID(boot["adminPrincipal"]["id"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )
    ws_a_id = uuid.UUID(ws_a["id"])
    async with app.state.session_factory() as session:
        for foreign in (uuid.UUID(in_b["id"]), uuid.uuid4()):
            with pytest.raises(NotFoundError):
                await create_goal(
                    session, ctx, title="Child", workspace_id=ws_a_id, parent_goal_id=foreign
                )
            goal = await session.get(Goal, uuid.UUID(in_a["id"]))
            assert goal is not None
            with pytest.raises(NotFoundError):
                await update_goal(
                    session, ctx, goal_id=goal.id, expected_version=1, parent_goal_id=foreign
                )
            with pytest.raises(NotFoundError):
                await create_task_command(
                    session, ctx, title="Sneaky", workspace_id=ws_a_id, goal_id=foreign
                )
        listed = {str(g.id) for g in (await list_goals(session, ctx)).items}
        await session.rollback()
    assert listed == {in_a["id"], tenant_level["id"]}


# --- origin -------------------------------------------------------------------


async def test_origin_is_derived_when_omitted(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, name="runner")

    by_person = await create_task(client, admin_key, title="Filed by a person")
    by_agent = await create_task(client, agent_key, title="Filed by an agent")
    subtask = await create_task(
        client, agent_key, title="Decomposed", parentTask=by_person["publicId"]
    )

    assert by_person["origin"] == {"kind": "human", "evidence": []}
    assert by_agent["origin"] == {"kind": "harness", "evidence": []}
    assert subtask["origin"] == {
        "kind": "parent",
        "ref": f"task:{by_person['id']}",
        "evidence": [],
    }
    assert by_person["goalId"] is None
    assert by_person["acceptance"] == []
    assert by_person["evidence"] == []


async def test_rule_origin_carries_its_rule_and_the_facts_it_fired_on(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    goal = await make_goal(client, admin_key, criteria=CRITERIA)
    source = await create_task(client, admin_key, title="Holds the report")
    observation_id = await observe(client, admin_key, "commit-abc123")
    artifact_id = await artifact(client, admin_key, source["publicId"])

    origin = {
        "kind": "rule",
        "ruleId": "decision-drift/v1",
        "ref": "decision:0048",
        "evidence": [
            {
                "kind": "observation",
                "observationId": observation_id,
                "note": "the change that diverged",
            },
            {"kind": "artifact", "artifactId": artifact_id},
            {"kind": "external", "externalRef": {"system": "vcs", "id": "abc123"}},
        ],
    }
    task = await create_task(
        client, admin_key, title="Restore conformance", goalId=goal["id"], origin=origin
    )
    assert task["goalId"] == goal["id"]
    assert task["origin"] == origin

    created = event_payloads(sync_engine, "task.created")[-1]
    assert created["goalId"] == goal["id"]
    # References travel; the note stays with the task.
    assert created["origin"] == {
        "kind": "rule",
        "ref": "decision:0048",
        "ruleId": "decision-drift/v1",
        "evidence": [
            {"kind": "observation", "observationId": observation_id},
            {"kind": "artifact", "artifactId": artifact_id},
            {"kind": "external", "externalRef": {"system": "vcs", "id": "abc123"}},
        ],
    }

    # Where the work came from is not editable.
    rewrite = await patch_task(client, admin_key, task, origin={"kind": "human"})
    assert rewrite.status_code == 400
    assert rewrite.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("origin", "code"),
    [
        (
            {
                "kind": "rule",
                "evidence": [{"kind": "external", "externalRef": {"system": "a", "id": "b"}}],
            },
            "invalid_origin",
        ),
        ({"kind": "rule", "ruleId": "drift"}, "invalid_origin"),
        ({"kind": "human", "ruleId": "drift"}, "invalid_origin"),
        ({"kind": "process"}, "invalid_origin"),
        ({"kind": "parent"}, "invalid_origin"),
        (
            {
                "kind": "harness",
                "evidence": [
                    {"kind": "external", "externalRef": {"system": "a", "id": "b"}, "check": "x"}
                ],
            },
            "invalid_origin",
        ),
    ],
)
async def test_invalid_origins_are_refused(
    client: httpx.AsyncClient, origin: dict[str, Any], code: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    refused = await client.post(
        "/api/v1/tasks", json={"title": "Bad origin", "origin": origin}, headers=auth(admin_key)
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == code


async def test_evidence_must_name_facts_of_this_tenant(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, other_key = make_tenant_directly(sync_engine, "other")
    foreign_observation = await observe(client, other_key, "elsewhere")
    unknown = "00000000-0000-4000-8000-000000000001"

    for item, code in (
        ({"kind": "observation", "observationId": foreign_observation}, "not_found"),
        ({"kind": "observation", "observationId": unknown}, "not_found"),
        ({"kind": "artifact", "artifactId": unknown}, "not_found"),
    ):
        refused = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Fires on nothing",
                "origin": {"kind": "rule", "ruleId": "drift", "evidence": [item]},
            },
            headers=auth(admin_key),
        )
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == code
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM tasks WHERE title = 'Fires on nothing'")
            ).scalar_one()
            == 0
        )


# --- acceptance and evidence --------------------------------------------------


async def test_acceptance_and_evidence_replace_and_stay_consistent(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    acceptance = [
        {"key": "tests-pass", "kind": "deterministic", "description": "The suite is green"},
        {
            "key": "reviewed",
            "kind": "llm_judge",
            "description": "A judge finds the change coherent",
            "spec": {"rubric": "coherence"},
        },
    ]
    task = await create_task(client, admin_key, title="Accepted by checks", acceptance=acceptance)
    assert task["acceptance"] == acceptance
    observation_id = await observe(client, admin_key, "ci-run-7")

    evidence = [{"kind": "observation", "observationId": observation_id, "check": "tests-pass"}]
    updated = await patch_task(client, admin_key, task, evidence=evidence)
    assert updated.status_code == 200, updated.text
    assert updated.json()["evidence"] == evidence
    assert event_payloads(sync_engine, "task.updated")[-1]["changes"] == {"evidence": 1}

    # Evidence for a check the task does not declare says nothing about it.
    stray = await patch_task(
        client,
        admin_key,
        updated.json(),
        evidence=[{"kind": "observation", "observationId": observation_id, "check": "deployed"}],
    )
    assert stray.status_code == 422
    assert stray.json()["error"]["code"] == "unknown_acceptance_check"
    # Nor may a check still cited by evidence be dropped from under it.
    dropped = await patch_task(client, admin_key, updated.json(), acceptance=acceptance[1:])
    assert dropped.status_code == 422
    assert dropped.json()["error"]["code"] == "unknown_acceptance_check"

    for body, code in (
        ({"acceptance": None}, "invalid_field"),
        ({"acceptance": [acceptance[0], acceptance[0]]}, "duplicate_check_key"),
        ({"acceptance": [{**acceptance[0], "kind": "vibes"}]}, "invalid_request"),
        (
            {"acceptance": [{**acceptance[1], "spec": {"apiToken": "x"}}]},
            "secret_material_rejected",
        ),
        # A check the verification stage could not execute is refused on write.
        (
            {"acceptance": [{**acceptance[0], "spec": {"suite": "conformance"}}]},
            "invalid_acceptance_spec",
        ),
        (
            {"evidence": [evidence[0], evidence[0]]},
            "duplicate_evidence",
        ),
    ):
        refused = await patch_task(client, admin_key, updated.json(), **body)
        assert refused.status_code in (400, 422), refused.text
        assert refused.json()["error"]["code"] == code

    cleared = await patch_task(client, admin_key, updated.json(), evidence=[], acceptance=[])
    assert cleared.status_code == 200
    assert (cleared.json()["acceptance"], cleared.json()["evidence"]) == ([], [])


# --- the work of a goal -------------------------------------------------------


async def test_work_of_a_goal_and_of_its_subgoals(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    goal = await make_goal(client, admin_key, title="Parent")
    subgoal = await make_goal(client, admin_key, title="Sub", parentGoalId=goal["id"])
    direct = await create_task(client, admin_key, title="Direct", goalId=goal["id"])
    nested = await create_task(client, admin_key, title="Nested", goalId=subgoal["id"])
    await create_task(client, admin_key, title="Unrelated")

    own = await client.get(f"/api/v1/goals/{goal['id']}/work", headers=auth(admin_key))
    assert own.status_code == 200
    assert [t["id"] for t in own.json()["items"]] == [direct["id"]]
    tree = await client.get(
        f"/api/v1/goals/{goal['id']}/work",
        params={"includeSubgoals": "true"},
        headers=auth(admin_key),
    )
    assert [t["id"] for t in tree.json()["items"]] == [nested["id"], direct["id"]]
    filtered = await client.get(
        "/api/v1/tasks", params={"goalId": subgoal["id"]}, headers=auth(admin_key)
    )
    assert [t["id"] for t in filtered.json()["items"]] == [nested["id"]]

    # Relinking and unlinking are ordinary updates.
    moved = await patch_task(client, admin_key, nested, goalId=goal["id"])
    assert moved.json()["goalId"] == goal["id"]
    unlinked = await patch_task(client, admin_key, moved.json(), goalId=None)
    assert unlinked.json()["goalId"] is None

    # An abandoned goal takes no new work; an achieved one still does.
    await patch_goal(client, admin_key, subgoal, status="abandoned")
    refused = await client.post(
        "/api/v1/tasks",
        json={"title": "Too late", "goalId": subgoal["id"]},
        headers=auth(admin_key),
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "goal_abandoned"
    await patch_goal(client, admin_key, goal, status="achieved")
    restore = await client.post(
        "/api/v1/tasks", json={"title": "Restore", "goalId": goal["id"]}, headers=auth(admin_key)
    )
    assert restore.status_code == 201, restore.text

    missing = await client.get(
        "/api/v1/goals/00000000-0000-4000-8000-000000000001/work", headers=auth(admin_key)
    )
    assert missing.status_code == 404


# --- migration ----------------------------------------------------------------


@pytest.fixture
def alembic_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_existing_tasks_become_human_originated_on_upgrade(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    goal = await make_goal(client, admin_key)
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="runner", permissions=["tasks.write", "goals.read"]
    )
    await create_task(client, agent_key, title="Linked", goalId=goal["id"])

    alembic_command.downgrade(alembic_config, BEFORE_WORK_GRAPH)
    with sync_engine.connect() as conn:
        assert "goals" not in inspect(conn).get_table_names()
        assert "origin" not in {c["name"] for c in inspect(conn).get_columns("tasks")}

    alembic_command.upgrade(alembic_config, WORK_GRAPH)
    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT origin, acceptance, evidence, goal_id FROM tasks")).all()
    # The downgrade is lossy (the harness origin and the link are gone); the
    # upgrade states the only origin a pre-M1.1 task could have had.
    assert rows == [({"kind": "human", "evidence": []}, [], [], None)]
