"""Skills an agent invokes, assigned by the registry (CP-ADR-0073, amendment 2026-09-28).

``spec.skills.invoke`` names the skill versions an agent calls through
``POST /skills/{ref}:invoke``. Publishing a revision brings ``principal_skills``
of the agent's principal to that list — assigning what is missing, taking back
only what the registry assigned — so a task type executed by a skill runs
under the agent without a hand-made assignment. The scenario is a bookkeeping
package on purpose: nothing here knows about software development.
"""

import copy
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests.helpers import (
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)
from tests.integration.test_skill_invocations_m21 import invoke, published

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"
ISSUER = "https://iam.example.test"
AGENT = "bookkeeper"
POST_INPUTS = {
    "type": "object",
    "properties": {"amount": {"type": "integer"}},
    "required": ["amount"],
}
RUNNER = ["sessions.open", "tasks.read", "tasks.write", "tasks.claim", "skills.invoke"]


def bookkeeper_spec(workspace: str, **skills: Any) -> dict[str, Any]:
    """The skills-executor example, taking the bookkeeping type of this tenant."""
    document = yaml.safe_load((FIXTURES / "skills-executor.yaml").read_text(encoding="utf-8"))
    spec: dict[str, Any] = copy.deepcopy(document["spec"])
    spec["displayName"] = "Bookkeeper"
    spec["identity"]["permissions"] = RUNNER
    spec["work"] = {"workspace": workspace, "taskTypes": ["post-entry"]}
    spec["skills"].update(skills)
    return spec


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin, "accounting")
    skills = {}
    for name in ("ledger.post", "ledger.close", "ledger.audit"):
        skills[name] = await published(
            client,
            admin,
            name,
            version="1",
            side_effects="external_write",
            risk_level="medium",
            inputs=POST_INPUTS,
            idempotency="natural",
        )
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "post-entry",
            "displayName": "Post a ledger entry",
            "execution": {"skill": "ledger.post", "version": "1"},
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    return {"admin": admin, "workspace": workspace, "skills": skills}


async def _publish(
    client: httpx.AsyncClient, key: str, spec: dict[str, Any], *, validate: bool = False
) -> httpx.Response:
    path = "/api/v1/agents:validate" if validate else "/api/v1/agents"
    return await client.post(path, json={"key": AGENT, "spec": spec}, headers=auth(key))


async def _link(client: httpx.AsyncClient, key: str) -> str:
    response = await client.put(
        f"/api/v1/agents/{AGENT}/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    principal_id: str = response.json()["principalId"]
    return principal_id


async def _assigned(client: httpx.AsyncClient, key: str, principal_id: str) -> dict[str, dict]:
    response = await client.get(f"/api/v1/principals/{principal_id}/skills", headers=auth(key))
    assert response.status_code == 200, response.text
    return {item["skill"]["name"]: item["metadata"] for item in response.json()["items"]}


async def _skill_events(
    client: httpx.AsyncClient, key: str, principal_id: str
) -> list[tuple[str, str]]:
    response = await client.get(
        "/api/v1/events",
        params={"types": "skill.assigned,skill.revoked"},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    return [
        (event["type"], event["payload"]["skillId"])
        for event in response.json()["items"]
        if event["entityId"] == principal_id
    ]


async def test_invoked_skills_are_checked_like_every_reference(client: httpx.AsyncClient) -> None:
    env = await _setup(client)
    admin, workspace = env["admin"], env["workspace"]["id"]

    cases: list[tuple[dict[str, Any], int, str, dict[str, Any]]] = [
        (
            bookkeeper_spec(workspace, invoke=["ledger.post@9"]),
            422,
            "unknown_reference",
            {"path": "spec.skills.invoke[0]", "value": "ledger.post@9"},
        ),
        # The type the agent takes is executed by a skill it does not invoke.
        (
            bookkeeper_spec(workspace, invoke=["ledger.close@1"]),
            422,
            "execution_skill_not_invoked",
            {
                "path": "spec.work.taskTypes[0]",
                "taskType": "post-entry",
                "skill": "ledger.post@1",
                "expected": "spec.skills.invoke",
            },
        ),
        (
            {
                **bookkeeper_spec(workspace, invoke=["ledger.post@1"]),
                "identity": {"kind": "agent", "permissions": ["tasks.read", "tasks.claim"]},
            },
            422,
            "skills_invoke_not_permitted",
            {"path": "spec.identity.permissions", "missing": ["skills.invoke"]},
        ),
    ]
    for spec, status, code, details in cases:
        for validate in (True, False):  # :validate answers like POST
            response = await _publish(client, admin, spec, validate=validate)
            assert response.status_code == status, (code, response.text)
            assert response.json()["error"]["code"] == code
            assert response.json()["error"]["details"] == details

    disabled = env["skills"]["ledger.audit"]
    await client.patch(
        f"/api/v1/skills/{disabled['id']}",
        json={"status": "disabled"},
        headers={**auth(admin), "If-Match": '"skill-1"'},
    )
    response = await _publish(
        client, admin, bookkeeper_spec(workspace, invoke=["ledger.post@1", "ledger.audit@1"])
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "skill_disabled"

    # Assigning a skill is org.manage, as assigning a role is.
    _, narrow = await create_agent_with_key(
        client, admin, name="packager", permissions=["agents.manage", *RUNNER]
    )
    response = await _publish(client, narrow, bookkeeper_spec(workspace, invoke=["ledger.post@1"]))
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_escalation"

    assert (await client.get(f"/api/v1/agents/{AGENT}", headers=auth(admin))).status_code == 404


async def test_a_revision_assigns_and_takes_back_its_own_skills(
    client: httpx.AsyncClient,
) -> None:
    env = await _setup(client)
    admin, workspace, skills = env["admin"], env["workspace"]["id"], env["skills"]
    post, close, audit = (skills[n]["id"] for n in ("ledger.post", "ledger.close", "ledger.audit"))

    first = bookkeeper_spec(workspace, invoke=["ledger.post@1", "ledger.close@1"])
    assert (await _publish(client, admin, first)).status_code == 201
    principal_id = await _link(client, admin)
    marked = {"assignedBy": "agent-registry"}
    assert await _assigned(client, admin, principal_id) == {
        "ledger.post": marked,
        "ledger.close": marked,
    }

    # A hand-made assignment is not the registry's to take back.
    await assign_skill(client, admin, principal_id, audit)
    events_before = len(await _skill_events(client, admin, principal_id))

    second = bookkeeper_spec(workspace, invoke=["ledger.post@1"])
    response = await _publish(client, admin, second)
    assert response.status_code == 201, response.text
    assert response.json()["currentRevision"] == 2
    assert await _assigned(client, admin, principal_id) == {
        "ledger.post": marked,
        "ledger.audit": {},
    }
    events = await _skill_events(client, admin, principal_id)
    assert events[events_before:] == [("skill.revoked", close)]
    published = (
        await client.get(
            "/api/v1/events", params={"types": "agent.revision_published"}, headers=auth(admin)
        )
    ).json()["items"]
    assert published[-1]["payload"]["permissionsChanged"] is True

    # The same spec again: no revision, nothing reassigned.
    assert (await _publish(client, admin, second)).json()["currentRevision"] == 2
    assert len(await _skill_events(client, admin, principal_id)) == len(events)

    third = bookkeeper_spec(workspace, invoke=["ledger.post@1", "ledger.close@1"])
    assert (await _publish(client, admin, third)).status_code == 201
    assert (await _skill_events(client, admin, principal_id))[-1] == ("skill.assigned", close)
    assert set(await _assigned(client, admin, principal_id)) == {
        "ledger.post",
        "ledger.close",
        "ledger.audit",
    }
    assert post in {skill_id for _, skill_id in events}


async def test_the_run_of_an_executed_type_invokes_without_a_manual_assignment(
    client: httpx.AsyncClient,
) -> None:
    """Acceptance: a clean tenant, a package applied, the work done — no hand-made call."""
    env = await _setup(client)
    admin, workspace = env["admin"], env["workspace"]["id"]
    agent = (
        await _publish(client, admin, bookkeeper_spec(workspace, invoke=["ledger.post@1"]))
    ).json()
    principal_id = await _link(client, admin)
    response = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": RUNNER},
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    agent_key = response.json()["key"]

    task = await create_task(
        client,
        admin,
        title="Post the invoice",
        typeKey="post-entry",
        workspaceId=workspace,
        customFields={"amount": 120},
    )
    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    run = await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
            "agentRevisionId": agent["revision"]["id"],
        },
        headers=auth(agent_key),
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    called = await invoke(
        client,
        agent_key,
        "ledger.post@1",
        {"amount": 120},
        runId=run_id,
        idempotencyKey=f"execution:{run_id}",
    )
    assert called.status_code == 201, called.text
    assert called.json()["authorizationBasis"]["kind"] == "execution"

    # Under the run the assignment is the gate: a skill the revision does not
    # name stays out of reach — exactly the failure the hand-made step hid.
    other = await invoke(client, agent_key, "ledger.close@1", {"amount": 1}, runId=run_id)
    assert other.status_code == 403, other.text
    assert other.json()["error"]["code"] == "tool_not_authorized"
