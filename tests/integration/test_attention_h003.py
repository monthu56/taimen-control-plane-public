"""``GET /me/attention`` and feedback on it (CP-ADR-0071, human-harness H003).

The same scenario runs on two domains installed as data through the public
API — software changes (``software-change``) and supplier invoice payment
(``invoice-payment``): a task type with its own status vocabulary and a role
that decides. The rules read only what core knows about any domain — status
categories, due dates, addressing of approvals, runs — so the second domain
passes without a line of code for it.
"""

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.common import utcnow
from control_plane.application.queries import attention
from tests.catalog_packages import install_package
from tests.helpers import (
    assign_role,
    auth,
    claim_task,
    create_agent_with_key,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)

DOMAINS: dict[str, dict[str, str]] = {
    "software-change": {
        "type": "code-change",
        "role": "code-reviewer",
        "blocked": "waiting_upstream",
        "cancelled": "dropped",
    },
    "invoice-payment": {
        "type": "invoice-payment",
        "role": "finance-director",
        "blocked": "on_hold",
        "cancelled": "withdrawn",
    },
}
HUMAN_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "approvals.read",
    "approvals.decide",
]
RUNNER_PERMISSIONS = ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]


def _in(hours: float) -> str:
    return (utcnow() + timedelta(hours=hours)).isoformat()


async def _approval(client: httpx.AsyncClient, key: str, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/approvals", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    return response.json()


async def _fail_runs(client: httpx.AsyncClient, key: str, task_id: str, times: int) -> None:
    session = await open_session(client, key)
    claim = (await claim_task(client, key, task_id, session["id"])).json()
    for attempt in range(times):
        run = await client.post(
            f"/api/v1/tasks/{task_id}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(key),
        )
        assert run.status_code == 201, run.text
        failed = await client.post(
            f"/api/v1/runs/{run.json()['id']}:fail",
            json={"failureReason": f"attempt {attempt + 1} failed"},
            headers=auth(key),
        )
        assert failed.status_code == 200, failed.text


async def _attention(client: httpx.AsyncClient, key: str, **params: str) -> dict[str, Any]:
    response = await client.get("/api/v1/me/attention", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _entries(body: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(item["rule"], item["reasonCode"], item["entity"]["id"]) for item in body["items"]}


async def _world(client: httpx.AsyncClient, domain: str) -> dict[str, Any]:
    """Two people of one tenant, an agent runner and work of every kind for both."""
    spec = DOMAINS[domain]
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, admin, "main")
    other = await create_workspace(client, admin, "other")
    other_child = await create_workspace(client, admin, "other-child", parent_id=other["id"])
    installed = await install_package(client, admin, domain, kinds={"Role", "TaskType"})
    role = installed[f"Role/{spec['role']}"]["id"]
    foreign_role = (await create_role(client, admin, "someone-elses"))["id"]

    me, me_key = await create_agent_with_key(
        client, admin, name="me", permissions=HUMAN_PERMISSIONS, kind="human"
    )
    stranger, stranger_key = await create_agent_with_key(
        client, admin, name="stranger", permissions=HUMAN_PERMISSIONS, kind="human"
    )
    runner, runner_key = await create_agent_with_key(
        client, admin, name="runner", permissions=RUNNER_PERMISSIONS
    )
    # I decide for the domain role everywhere; the stranger only under "other".
    await assign_role(client, admin, me["id"], role)
    await assign_role(client, admin, stranger["id"], role, workspace_id=other["id"])
    await assign_role(client, admin, stranger["id"], foreign_role)

    async def task(
        title: str, workspace: dict[str, Any] = ws, status: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        created = await create_task(
            client, admin, title=title, typeKey=spec["type"], workspaceId=workspace["id"], **extra
        )
        if status is None:
            return created
        # New work starts in the initial status; the domain's own key moves it.
        moved = await client.patch(
            f"/api/v1/tasks/{created['id']}",
            json={"status": status},
            headers={**auth(admin), "If-Match": f'"task-{created["version"]}"'},
        )
        assert moved.status_code == 200, moved.text
        body: dict[str, Any] = moved.json()
        return body

    t = {
        "for_me": await task("Assigned decision"),
        "for_role": await task("Role decision"),
        "review": await task("Waiting on review"),
        "due_soon": await task("Due tomorrow", assigneeId=me["id"], dueDate=_in(24)),
        "overdue": await task("Overdue", ownerId=me["id"], dueDate=_in(-2)),
        "due_later": await task("Due next week", assigneeId=me["id"], dueDate=_in(24 * 5)),
        "due_started": await task("Due but started", assigneeId=me["id"], dueDate=_in(12)),
        "due_gone": await task(
            "Due but cancelled", status=spec["cancelled"], assigneeId=me["id"], dueDate=_in(6)
        ),
        "blocked": await task("Blocked", other, ownerId=me["id"], status=spec["blocked"]),
        "failing": await task(
            "Delegated and failing", other_child, ownerId=me["id"], assigneeId=runner["id"]
        ),
        "failing_twice": await task(
            "Delegated, failed twice", ownerId=me["id"], assigneeId=runner["id"]
        ),
        "s_due": await task("Stranger's deadline", assigneeId=stranger["id"], dueDate=_in(3)),
        "s_blocked": await task(
            "Stranger's blocked", assigneeId=stranger["id"], status=spec["blocked"]
        ),
        "s_failing": await task(
            "Stranger's delegation", ownerId=stranger["id"], assigneeId=runner["id"]
        ),
    }
    session = await open_session(client, me_key)
    claimed = await claim_task(client, me_key, t["due_started"]["id"], session["id"])
    assert claimed.status_code == 200, claimed.text
    await _fail_runs(client, runner_key, t["failing"]["id"], 3)
    await _fail_runs(client, runner_key, t["failing_twice"]["id"], 2)
    await _fail_runs(client, runner_key, t["s_failing"]["id"], 3)

    a = {
        "for_me": await _approval(
            client, admin, task=t["for_me"]["id"], assignedPrincipalId=me["id"]
        ),
        "for_role": await _approval(client, admin, task=t["for_role"]["id"], requiredRoleId=role),
        "review": await _approval(
            client, admin, task=t["review"]["id"], requiredRoleId=role, gate=True
        ),
        "shared": await _approval(
            client, admin, requiredRoleId=role, workspaceId=other["id"], comment="Shared"
        ),
        "s_assigned": await _approval(
            client, admin, assignedPrincipalId=stranger["id"], comment="Not mine"
        ),
        "s_role": await _approval(
            client, admin, requiredRoleId=foreign_role, comment="Not my role"
        ),
    }
    decided = await _approval(client, admin, assignedPrincipalId=me["id"], comment="Decided")
    response = await client.post(
        f"/api/v1/approvals/{decided['id']}:approve", json={}, headers=auth(me_key)
    )
    assert response.status_code == 200, response.text
    return {
        "admin": admin,
        "me_key": me_key,
        "stranger_key": stranger_key,
        "ws": ws,
        "other": other,
        "t": t,
        "a": a,
    }


@pytest.mark.parametrize("domain", sorted(DOMAINS))
async def test_items_for_the_owner_follow_the_rules(client: httpx.AsyncClient, domain: str) -> None:
    w = await _world(client, domain)
    t, a = w["t"], w["a"]

    body = await _attention(client, w["me_key"])

    assert _entries(body) == {
        ("approval.decide@1", "decision_assigned", a["for_me"]["id"]),
        ("approval.decide@1", "decision_role", a["for_role"]["id"]),
        ("approval.decide@1", "decision_role", a["shared"]["id"]),
        ("approval.review@1", "review_role", a["review"]["id"]),
        ("task.due_not_started@1", "due_soon", t["due_soon"]["id"]),
        ("task.due_not_started@1", "overdue", t["overdue"]["id"]),
        ("task.blocked@1", "task_blocked", t["blocked"]["id"]),
        ("task.delegated_failing@1", "runs_failed", t["failing"]["id"]),
    }
    assert body["degraded"] == []
    scores = [item["score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= score <= 100 for score in scores)

    by_entity = {item["entity"]["id"]: item for item in body["items"]}
    review = by_entity[a["review"]["id"]]
    assert review["kind"] == "review"
    assert review["itemKey"] == f"approval.review:{a['review']['id']}"
    assert review["taskPublicId"] == t["review"]["publicId"]
    assert {action["action"] for action in review["actions"]} == {
        "approve",
        "reject",
        "open",
        "openTask",
        "feedback",
    }
    # A check waiting on the principal outranks a plain decision.
    assert review["score"] > by_entity[a["for_role"]["id"]]["score"]
    overdue = by_entity[t["overdue"]["id"]]
    assert overdue["kind"] == "deadline"
    assert overdue["score"] > by_entity[t["due_soon"]["id"]]["score"]
    failing = by_entity[t["failing"]["id"]]
    assert failing["kind"] == "delegated_failure"
    assert failing["details"]["failedRuns"] == 3
    assert failing["details"]["lastFailureReason"] == "attempt 3 failed"
    blocked = by_entity[t["blocked"]["id"]]
    assert blocked["details"]["status"] == DOMAINS[domain]["blocked"]
    assert blocked["workspaceId"] == w["other"]["id"]
    assert all(item["feedback"] is None for item in body["items"])


@pytest.mark.parametrize("domain", sorted(DOMAINS))
async def test_someone_elses_objects_are_not_on_the_list(
    client: httpx.AsyncClient, domain: str
) -> None:
    w = await _world(client, domain)
    t, a = w["t"], w["a"]

    mine = {item["entity"]["id"] for item in (await _attention(client, w["me_key"]))["items"]}
    for foreign in (t["s_due"], t["s_blocked"], t["s_failing"], a["s_assigned"], a["s_role"]):
        assert foreign["id"] not in mine

    # The stranger's own list: its objects, and the shared approval its scoped
    # role reaches — none of mine.
    theirs = await _attention(client, w["stranger_key"])
    assert _entries(theirs) == {
        ("approval.decide@1", "decision_assigned", a["s_assigned"]["id"]),
        ("approval.decide@1", "decision_role", a["s_role"]["id"]),
        ("approval.decide@1", "decision_role", a["shared"]["id"]),
        ("task.due_not_started@1", "due_soon", t["s_due"]["id"]),
        ("task.blocked@1", "task_blocked", t["s_blocked"]["id"]),
        ("task.delegated_failing@1", "runs_failed", t["s_failing"]["id"]),
    }

    # Feedback on someone else's item is refused as if the item did not exist.
    key = f"approval.decide:{a['s_assigned']['id']}"
    response = await client.post(
        f"/api/v1/me/attention/{key}:feedback",
        json={"verdict": "not_needed"},
        headers=auth(w["me_key"]),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_workspace_filter_and_descendants(client: httpx.AsyncClient) -> None:
    w = await _world(client, "software-change")
    t, a, other = w["t"], w["a"], w["other"]

    only = await _attention(client, w["me_key"], workspaceId=other["id"])
    assert {i["entity"]["id"] for i in only["items"]} == {t["blocked"]["id"], a["shared"]["id"]}

    subtree = await _attention(
        client, w["me_key"], workspaceId=other["id"], includeDescendants="true"
    )
    assert {i["entity"]["id"] for i in subtree["items"]} == {
        t["blocked"]["id"],
        a["shared"]["id"],
        t["failing"]["id"],
    }

    unknown = await client.get(
        "/api/v1/me/attention",
        params={"workspaceId": "00000000-0000-4000-8000-000000000000"},
        headers=auth(w["me_key"]),
    )
    assert unknown.status_code == 404
    strict = await client.get(
        "/api/v1/me/attention", params={"limit": "5"}, headers=auth(w["me_key"])
    )
    assert strict.status_code == 400


async def test_feedback_is_recorded_replaced_and_shown(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    w = await _world(client, "invoice-payment")
    item = next(
        i
        for i in (await _attention(client, w["me_key"]))["items"]
        if i["entity"]["id"] == w["t"]["blocked"]["id"]
    )
    url = f"/api/v1/me/attention/{item['itemKey']}:feedback"

    first = await client.post(
        url, json={"verdict": "not_needed", "comment": "I know"}, headers=auth(w["me_key"])
    )
    assert first.status_code == 201, first.text
    assert first.json()["itemKey"] == item["itemKey"]
    assert first.json()["rule"] == "task.blocked@1"
    assert first.json()["verdict"] == "not_needed"

    again = await client.post(url, json={"verdict": "useful"}, headers=auth(w["me_key"]))
    assert again.status_code == 200, again.text

    shown = next(
        i
        for i in (await _attention(client, w["me_key"]))["items"]
        if i["itemKey"] == item["itemKey"]
    )
    # Feedback does not hide the item: the rule decides what is shown (§4).
    assert shown["feedback"]["verdict"] == "useful"

    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT verdict, rule_key, rule_version, score FROM attention_feedback")
        ).all()
        events = (
            conn.execute(
                text(
                    "SELECT payload FROM events WHERE event_type = 'attention.feedback_recorded'"
                    " ORDER BY sequence"
                )
            )
            .scalars()
            .all()
        )
    assert [tuple(row) for row in rows] == [("useful", "task.blocked", 1, item["score"])]
    assert [(e["verdict"], e["created"], e["hasComment"]) for e in events] == [
        ("not_needed", True, True),
        ("useful", False, False),
    ]
    assert events[0]["rule"] == "task.blocked@1"
    assert "I know" not in str(events)

    for bad_key in ("nonsense", "task.blocked:not-a-uuid", f"no.such.rule:{item['entity']['id']}"):
        response = await client.post(
            f"/api/v1/me/attention/{bad_key}:feedback",
            json={"verdict": "useful"},
            headers=auth(w["me_key"]),
        )
        assert response.status_code == 404, bad_key
    invalid = await client.post(url, json={"verdict": "meh"}, headers=auth(w["me_key"]))
    assert invalid.status_code == 400


async def test_a_missing_permission_degrades_its_rules(client: httpx.AsyncClient) -> None:
    w = await _world(client, "software-change")
    _, tasks_only = await create_agent_with_key(
        client, w["admin"], name="reader", permissions=["tasks.read"], kind="human"
    )
    body = await _attention(client, tasks_only)
    assert {(d["rule"], d["reasonCode"]) for d in body["degraded"]} == {
        ("approval.review@1", "permission_missing"),
        ("approval.decide@1", "permission_missing"),
    }

    _, nothing = await create_agent_with_key(
        client, w["admin"], name="nobody", permissions=["events.read"], kind="human"
    )
    response = await client.get("/api/v1/me/attention", headers=auth(nothing))
    assert response.status_code == 403


async def test_a_failing_rule_degrades_the_list_not_the_answer(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, "invoice-payment")

    async def broken(
        session: AsyncSession, scope: attention.Scope, rule: attention.AttentionRule, limit: int
    ) -> list[attention.AttentionItem]:
        # A database error aborts the rule's savepoint, not the transaction.
        await session.execute(text("SELECT 1 / 0"))
        return []

    rules = tuple(
        attention.AttentionRule(r.key, r.version, r.permission, broken)
        if r.key == "task.blocked"
        else r
        for r in attention.RULES
    )
    monkeypatch.setattr(attention, "RULES", rules)

    body = await _attention(client, w["me_key"])
    assert [(d["rule"], d["reasonCode"]) for d in body["degraded"]] == [
        ("task.blocked@1", "rule_failed")
    ]
    assert w["t"]["blocked"]["id"] not in {i["entity"]["id"] for i in body["items"]}
    assert w["a"]["review"]["id"] in {i["entity"]["id"] for i in body["items"]}

    # Feedback on an item of a rule that cannot be evaluated is 503, not 404.
    response = await client.post(
        f"/api/v1/me/attention/task.blocked:{w['t']['blocked']['id']}:feedback",
        json={"verdict": "useful"},
        headers=auth(w["me_key"]),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "attention_rule_unavailable"
