"""End-to-end v0.2 scenario (spec §29): one coordination protocol for
humans and agents — workspace, org profile, requirements, claim, run,
artifact, completion, and a role-gated human approval."""

import httpx

from tests.helpers import (
    assign_capability,
    assign_role,
    assign_skill,
    auth,
    claim_task,
    create_agent_with_key,
    create_capability,
    create_role,
    create_workspace,
    do_bootstrap,
    open_session,
    register_skill,
)


async def test_org_scenario(client: httpx.AsyncClient) -> None:
    # --- administrator sets up the organization ------------------------------
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    engineering = await create_workspace(client, admin_key, "engineering")
    platform = await create_workspace(client, admin_key, "platform", parent_id=engineering["id"])

    role = await create_role(client, admin_key, "software-engineer")
    cap_python = await create_capability(client, admin_key, "code.python")
    cap_github = await create_capability(client, admin_key, "github.write")
    skill_pr = await register_skill(client, admin_key, "github.create_pr", protocol="mcp")

    # --- the agent principal gets its organizational profile ------------------
    agent, agent_key = await create_agent_with_key(
        client,
        admin_key,
        name="coding-agent",
        permissions=[
            "sessions.open",
            "tasks.read",
            "tasks.write",
            "tasks.claim",
            "events.read",
            "artifacts.read",
            "artifacts.write",
            "approvals.manage",
        ],
    )
    await assign_role(client, admin_key, agent["id"], role["id"], workspace_id=engineering["id"])
    await assign_capability(client, admin_key, agent["id"], cap_python["id"])
    await assign_capability(client, admin_key, agent["id"], cap_github["id"])
    await assign_skill(client, admin_key, agent["id"], skill_pr["id"])

    # --- the task, with requirements instead of a concrete assignee -----------
    task = (
        await client.post(
            "/api/v1/tasks",
            json={
                "title": "Fix regression #483",
                "workspaceId": platform["id"],
                "requirements": {
                    "roles": ["software-engineer"],
                    "capabilities": ["code.python", "github.write"],
                    "skills": ["github.create_pr"],
                },
            },
            headers=auth(admin_key),
        )
    ).json()

    # --- the agent works through the unified protocol -------------------------
    session = await open_session(client, agent_key, client_name="opencode-runtime")
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    assert claim["fencingToken"] == 1

    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={
                "claimId": claim["id"],
                "fencingToken": claim["fencingToken"],
                "input": {"issue": 483},
            },
            headers=auth(agent_key),
        )
    ).json()
    assert run["status"] == "running"

    artifact = (
        await client.post(
            "/api/v1/artifacts",
            json={
                "type": "github.pull_request",
                "name": "Fix regression #483",
                "runId": run["id"],
                "uri": "https://github.com/acme/repo/pull/484",
            },
            headers=auth(agent_key),
        )
    ).json()
    assert artifact["taskId"] == task["id"]

    finished = (
        await client.post(
            f"/api/v1/runs/{run['id']}:succeed",
            json={"output": {"pr": 484}},
            headers=auth(agent_key),
        )
    ).json()
    assert finished["run"]["status"] == "succeeded"
    assert finished["task"]["status"] == "done"

    # --- human-in-the-loop: agent requests a role-gated approval --------------
    legal_role = await create_role(client, admin_key, "legal-lead")
    human, human_key = await create_agent_with_key(  # kind differs, protocol doesn't
        client,
        admin_key,
        name="legal-lead-human",
        permissions=["approvals.read", "approvals.decide"],
    )
    await assign_role(client, admin_key, human["id"], legal_role["id"])

    review = (
        await client.post(
            "/api/v1/artifacts",
            json={"type": "contract.review", "name": "MSA review", "task": task["id"]},
            headers=auth(agent_key),
        )
    ).json()
    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"artifactId": review["id"], "requiredRoleId": legal_role["id"]},
            headers=auth(agent_key),
        )
    ).json()

    decided = (
        await client.post(
            f"/api/v1/approvals/{approval['id']}:approve",
            json={"comment": "Approved after legal review"},
            headers=auth(human_key),
        )
    ).json()
    assert decided["status"] == "approved"
    assert decided["decisionByPrincipalId"] == human["id"]

    # --- the journal tells the whole story ------------------------------------
    events = (await client.get("/api/v1/events?limit=100", headers=auth(agent_key))).json()["items"]
    types = [e["type"] for e in events]
    for expected in (
        "workspace.created",
        "role.created",
        "capability.created",
        "skill.registered",
        "role.assigned",
        "capability.assigned",
        "skill.assigned",
        "task.created",
        "task.claimed",
        "run.started",
        "artifact.created",
        "run.succeeded",
        "claim.released",
        "task.completed",
        "approval.requested",
        "approval.approved",
    ):
        assert expected in types, expected
