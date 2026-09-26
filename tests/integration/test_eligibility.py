"""Task requirements and claim eligibility (ALL requirements are mandatory)."""

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
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
    register_skill,
)


async def _agent_with_session(
    client: httpx.AsyncClient, admin_key: str, name: str = "agent"
) -> tuple[dict, str, dict]:
    principal, key = await create_agent_with_key(client, admin_key, name=name)
    session = await open_session(client, key, client_name=name)
    return principal, key, session


async def test_eligibility_all_requirements_mandatory(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, agent_key, session = await _agent_with_session(client, admin_key)

    role = await create_role(client, admin_key, "software-engineer")
    capability = await create_capability(client, admin_key, "code.python")
    skill = await register_skill(client, admin_key, "github.create_pr")

    task = await create_task(
        client,
        admin_key,
        requirements={
            "roles": ["software-engineer"],
            "capabilities": ["code.python"],
            "skills": ["github.create_pr"],
        },
    )

    # Requirements are visible
    requirements = (
        await client.get(f"/api/v1/tasks/{task['id']}/requirements", headers=auth(admin_key))
    ).json()
    assert requirements["roles"][0]["slug"] == "software-engineer"
    assert requirements["capabilities"][0]["name"] == "code.python"
    assert requirements["skills"][0]["name"] == "github.create_pr"

    # Nothing assigned -> not eligible, all three reported missing
    response = await claim_task(client, agent_key, task["id"], session["id"])
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "not_eligible"
    assert error["details"]["missingRoles"] == ["software-engineer"]
    assert error["details"]["missingCapabilities"] == ["code.python"]
    assert error["details"]["missingSkills"] == ["github.create_pr"]

    # Partial satisfaction is still not enough
    await assign_role(client, admin_key, principal["id"], role["id"])
    await assign_capability(client, admin_key, principal["id"], capability["id"])
    response = await claim_task(client, agent_key, task["id"], session["id"])
    assert response.status_code == 403
    assert response.json()["error"]["details"]["missingSkills"] == ["github.create_pr"]

    # Full satisfaction -> claim succeeds
    await assign_skill(client, admin_key, principal["id"], skill["id"])
    response = await claim_task(client, agent_key, task["id"], session["id"])
    assert response.status_code == 200, response.text


async def test_task_without_requirements_is_open(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _agent_with_session(client, admin_key)
    task = await create_task(client, admin_key)
    assert (await claim_task(client, agent_key, task["id"], session["id"])).status_code == 200


async def test_role_scope_subtree_semantics(client: httpx.AsyncClient) -> None:
    """A role granted on a parent workspace applies to tasks in its subtree."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    engineering = await create_workspace(client, admin_key, "engineering")
    platform = await create_workspace(client, admin_key, "platform", parent_id=engineering["id"])
    unrelated = await create_workspace(client, admin_key, "sales")

    role = await create_role(client, admin_key, "software-engineer")
    principal, agent_key, session = await _agent_with_session(client, admin_key)
    task = await create_task(
        client,
        admin_key,
        workspaceId=platform["id"],
        requirements={"roles": ["software-engineer"]},
    )

    # Assignment scoped to an UNRELATED workspace does not satisfy
    await assign_role(client, admin_key, principal["id"], role["id"], workspace_id=unrelated["id"])
    assert (await claim_task(client, agent_key, task["id"], session["id"])).status_code == 403

    # Assignment on the ANCESTOR workspace covers the child task
    await assign_role(
        client, admin_key, principal["id"], role["id"], workspace_id=engineering["id"]
    )
    assert (await claim_task(client, agent_key, task["id"], session["id"])).status_code == 200


async def test_tenant_wide_role_assignment_satisfies_scoped_task(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "eng")
    role = await create_role(client, admin_key, "engineer")
    principal, agent_key, session = await _agent_with_session(client, admin_key)
    task = await create_task(
        client, admin_key, workspaceId=workspace["id"], requirements={"roles": ["engineer"]}
    )
    await assign_role(client, admin_key, principal["id"], role["id"])  # tenant-wide
    assert (await claim_task(client, agent_key, task["id"], session["id"])).status_code == 200


async def test_unknown_requirement_rejected_at_creation(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    for req in (
        {"roles": ["ghost-role"]},
        {"capabilities": ["ghost.capability"]},
        {"skills": ["ghost.skill"]},
    ):
        response = await client.post(
            "/api/v1/tasks",
            json={"title": "T", "requirements": req},
            headers=auth(admin_key),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "unknown_requirement"


async def test_skill_requirement_matches_by_name_across_versions(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    v1 = await register_skill(client, admin_key, "postgres.query", version="1.0.0")
    await register_skill(client, admin_key, "postgres.query", version="2.0.0")

    principal, agent_key, session = await _agent_with_session(client, admin_key)
    # Requirement resolves to the newest version, principal holds v1 — name match wins.
    task = await create_task(client, admin_key, requirements={"skills": ["postgres.query"]})
    await assign_skill(client, admin_key, principal["id"], v1["id"])
    assert (await claim_task(client, agent_key, task["id"], session["id"])).status_code == 200


async def test_requirements_replace_via_patch(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_role(client, admin_key, "a-role")
    await create_role(client, admin_key, "b-role")
    task = await create_task(client, admin_key, requirements={"roles": ["a-role"]})

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"requirements": {"roles": ["b-role"]}},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 200, response.text
    requirements = (
        await client.get(f"/api/v1/tasks/{task['id']}/requirements", headers=auth(admin_key))
    ).json()
    assert [r["slug"] for r in requirements["roles"]] == ["b-role"]


async def test_disabled_skill_assignment_does_not_satisfy_requirement(
    client: httpx.AsyncClient,
) -> None:
    """A held assignment to a later-DISABLED skill version is unusable."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    v1 = await register_skill(client, admin_key, "deploy", version="1.0.0")
    principal, agent_key, session = await _agent_with_session(client, admin_key)
    await assign_skill(client, admin_key, principal["id"], v1["id"])

    # An active v2 exists, so the requirement itself resolves fine.
    await register_skill(client, admin_key, "deploy", version="2.0.0")
    task = await create_task(client, admin_key, requirements={"skills": ["deploy"]})

    # While v1 is active the principal is eligible.
    assert (await claim_task(client, agent_key, task["id"], session["id"])).status_code == 200

    # Disable v1: the same assignment no longer satisfies the requirement.
    response = await client.patch(
        f"/api/v1/skills/{v1['id']}",
        json={"status": "disabled"},
        headers={**auth(admin_key), "If-Match": '"skill-1"'},
    )
    assert response.status_code == 200
    task2 = await create_task(
        client, admin_key, title="Second", requirements={"skills": ["deploy"]}
    )
    response = await claim_task(client, agent_key, task2["id"], session["id"])
    assert response.status_code == 403
    assert response.json()["error"]["details"]["missingSkills"] == ["deploy"]


async def test_requirements_null_rejected_empty_object_clears(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_role(client, admin_key, "gate-role")
    task = await create_task(client, admin_key, requirements={"roles": ["gate-role"]})

    # Explicit null is rejected loudly, not silently ignored.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Renamed", "requirements": None},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 422
    assert "empty object" in response.json()["error"]["message"]

    # An empty requirements object clears the set.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"requirements": {}},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert response.status_code == 200, response.text
    requirements = (
        await client.get(f"/api/v1/tasks/{task['id']}/requirements", headers=auth(admin_key))
    ).json()
    assert requirements == {"roles": [], "capabilities": [], "skills": []}

    # And the task is now claimable by anyone.
    _, agent_key, session = await _agent_with_session(client, admin_key, "late-agent")
    assert (await claim_task(client, agent_key, task["id"], session["id"])).status_code == 200
