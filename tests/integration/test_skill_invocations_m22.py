"""M2.2 server side: task_types.execution, the execution basis, cancellation,
the basis re-checked at claim, and the review items of M2.1 (ADR-0056)."""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import assign_skill, auth, create_agent_with_key, create_task, do_bootstrap
from tests.integration.test_child_run_handle import claim_and_run
from tests.integration.test_skill_invocation_authz import approved_gate
from tests.integration.test_skill_invocations_m21 import (
    claim,
    event_types,
    invoke,
    publish,
    published,
)

CALLER = ["skills.invoke", "tasks.read", "tasks.write"]
EXECUTOR = ["skills.execute", "sessions.open"]
RUNNER = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "skills.invoke",
    "skills.execute",
    "artifacts.write",
]
MERGE_INPUTS = {
    "type": "object",
    "properties": {"branch": {"type": "string"}},
    "required": ["branch"],
}


@pytest.fixture
async def boot(client: httpx.AsyncClient) -> dict[str, Any]:
    body = await do_bootstrap(client)
    admin = body["apiKey"]["key"]
    caller, caller_key = await create_agent_with_key(
        client, admin, name="caller", permissions=CALLER
    )
    _, executor_key = await create_agent_with_key(
        client, admin, name="executor", permissions=EXECUTOR
    )
    runner, runner_key = await create_agent_with_key(
        client, admin, name="runner", permissions=RUNNER
    )
    return {
        "admin": admin,
        "adminId": body["adminPrincipal"]["id"],
        "caller": caller_key,
        "callerId": caller["id"],
        "executor": executor_key,
        "runner": runner_key,
        "runnerId": runner["id"],
    }


async def task_type(
    client: httpx.AsyncClient, key: str, execution: Any, *, type_key: str = "merge"
) -> httpx.Response:
    return await client.post(
        "/api/v1/task-types",
        json={"key": type_key, "displayName": "Merge", "execution": execution},
        headers=auth(key),
    )


async def merge_skill(
    client: httpx.AsyncClient, boot: dict[str, Any], *, side_effects: str = "external_write"
) -> dict[str, Any]:
    skill = await published(
        client,
        boot["admin"],
        "git.merge",
        version="1",
        side_effects=side_effects,
        risk_level="high",
        inputs=MERGE_INPUTS,
        idempotency="natural",
    )
    await assign_skill(client, boot["admin"], boot["runnerId"], skill["id"])
    return skill


async def execution_run(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A running run of a task whose type is executed by git.merge@1."""
    response = await task_type(client, boot["admin"], {"skill": "git.merge", "version": "1"})
    assert response.status_code == 201, response.text
    task = await create_task(
        client, boot["admin"], title="Merge it", typeKey="merge", customFields={"branch": "b"}
    )
    _, run = await claim_and_run(client, boot["runner"], task["id"])
    return task, run


def status_of(sync_engine: Engine, invocation_id: str) -> tuple[str, dict[str, Any] | None]:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, error FROM skill_invocations WHERE id = :id"),
            {"id": invocation_id},
        ).one()
    return row[0], row[1]


# --- §3 task_types.execution ----------------------------------------------------


async def test_execution_is_validated_and_normalized(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    admin = boot["admin"]
    await merge_skill(client, boot)

    response = await task_type(client, admin, {"skill": "git.merge", "version": "1"})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["execution"] == {
        "skill": "git.merge",
        "version": "1",
        "inputs": "$.customFields",
    }
    fetched = await client.get(f"/api/v1/task-types/{created['id']}", headers=auth(admin))
    assert fetched.json()["execution"] == created["execution"]

    mapped = {"skill": "git.merge", "version": "1", "inputs": {"branch": "$.customFields.b[0]"}}
    response = await task_type(client, admin, mapped)
    assert response.status_code == 201, response.text
    assert response.json()["execution"]["inputs"] == {"branch": "$.customFields.b[0]"}

    for execution, reason in (
        ({"skill": "git.merge", "version": "9"}, "not_found"),
        ({"skill": "nope", "version": "1"}, "not_found"),
    ):
        response = await task_type(client, admin, execution)
        assert response.status_code == 422, response.text
        body = response.json()["error"]
        assert body["code"] == "invalid_task_execution"
        assert body["details"]["reason"] == reason
    for execution in (
        {"skill": "git.merge"},
        {"skill": "git.merge@1", "version": "1"},
        {"skill": "git.merge", "version": "1", "inputs": "customFields"},
        {"skill": "git.merge", "version": "1", "inputs": {"a": "$..x"}},
        {"skill": "git.merge", "version": "1", "extra": True},
    ):
        response = await task_type(client, admin, execution)
        assert response.status_code == 422, (execution, response.text)
        assert response.json()["error"]["code"] == "invalid_task_execution"

    # Not an object at all: the request contract already says no.
    assert (await task_type(client, admin, "git.merge@1")).status_code == 400

    catalog = await client.post(
        "/api/v1/skills",
        json={"name": "bidops.fetch", "version": "1", "protocol": "http"},
        headers=auth(admin),
    )
    assert catalog.status_code == 201
    response = await task_type(client, admin, {"skill": "bidops.fetch", "version": "1"})
    assert response.json()["error"]["details"]["reason"] == "no_contract"

    # The version is immutable, execution included.
    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_types SET execution = NULL WHERE id = :id"), {"id": created["id"]}
        )


# --- §4 the execution basis ----------------------------------------------------------


async def test_execution_is_a_basis_for_the_one_call_of_its_run(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    await merge_skill(client, boot)
    task, run = await execution_run(client, boot)

    first = await invoke(
        client,
        boot["runner"],
        "git.merge@1",
        {"branch": "b"},
        runId=run["id"],
        idempotencyKey=f"execution:{run['id']}",
    )
    assert first.status_code == 201, first.text
    basis = first.json()["authorizationBasis"]
    assert basis["kind"] == "execution"
    assert (basis["runId"], basis["taskId"]) == (run["id"], task["id"])

    repeat = await invoke(
        client,
        boot["runner"],
        "git.merge@1",
        {"branch": "b"},
        runId=run["id"],
        idempotencyKey=f"execution:{run['id']}",
    )
    assert repeat.status_code == 200
    assert repeat.json()["id"] == first.json()["id"]

    second = await invoke(client, boot["runner"], "git.merge@1", {"branch": "c"}, runId=run["id"])
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "execution_already_invoked"

    # Without its run the same call has no basis at all.
    direct = await invoke(client, boot["runner"], "git.merge@1", {"branch": "b"}, taskId=task["id"])
    assert direct.status_code == 403, direct.text
    assert direct.json()["error"]["code"] == "skill_side_effect_not_authorized"


async def test_execution_basis_needs_the_type_to_name_this_version(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await merge_skill(client, boot)
    other = await published(
        client,
        boot["admin"],
        "git.merge",
        version="2",
        side_effects="external_write",
        risk_level="high",
        inputs=MERGE_INPUTS,
    )
    await assign_skill(client, boot["admin"], boot["runnerId"], other["id"])
    _, run = await execution_run(client, boot)

    response = await invoke(client, boot["runner"], "git.merge@2", {"branch": "b"}, runId=run["id"])
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "skill_side_effect_not_authorized"


async def test_a_pending_gate_holds_the_execution_basis(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    await merge_skill(client, boot)
    task, run = await execution_run(client, boot)
    gate = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": boot["adminId"], "gate": True},
        headers=auth(boot["admin"]),
    )
    assert gate.status_code == 201, gate.text

    response = await invoke(client, boot["runner"], "git.merge@1", {"branch": "b"}, runId=run["id"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "approval_required"

    await client.post(f"/api/v1/approvals/{gate.json()['id']}:approve", headers=auth(boot["admin"]))
    response = await invoke(client, boot["runner"], "git.merge@1", {"branch": "b"}, runId=run["id"])
    assert response.status_code == 201, response.text


# --- review 1: the basis is checked again at claim ---------------------------------


async def test_claim_cancels_a_call_of_a_disabled_version(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    skill = await published(client, boot["admin"])
    created = (await invoke(client, boot["caller"], "repo.search", {"query": "x"})).json()
    await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"status": "disabled"},
        headers={**auth(boot["admin"]), "If-Match": '"skill-1"'},
    )

    assert (await claim(client, boot["executor"])).status_code == 204
    status, error = status_of(sync_engine, created["id"])
    assert status == "cancelled"
    assert error is not None
    assert (error["code"], error["message"]) == ("basis_revoked", "skill_disabled")
    # Nobody asked for it: the claim found the basis gone.
    assert error["details"]["initiator"] == "system"
    assert event_types(sync_engine, created["id"])[-1] == "skill.invocation_cancelled"


async def test_claim_cancels_an_approved_call_for_a_closed_task(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    admin = boot["admin"]
    await published(client, admin, "crm.update", side_effects="external_write", risk_level="high")
    task = await create_task(client, admin, title="Update CRM")
    gate = await approved_gate(client, boot, task["id"])
    created = await invoke(
        client, boot["caller"], "crm.update", {"query": "x"}, taskId=task["id"], approvalId=gate
    )
    assert created.status_code == 201, created.text
    current = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin))
    await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "cancelled"},
        headers={**auth(admin), "If-Match": current.headers["etag"]},
    )

    assert (await claim(client, boot["executor"])).status_code == 204
    status, error = status_of(sync_engine, created.json()["id"])
    assert status == "cancelled"
    assert error is not None and error["message"] == "task_terminal"


async def test_claim_cancels_the_call_of_a_finished_run_and_holds_a_gated_one(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    await merge_skill(client, boot)
    task, run = await execution_run(client, boot)
    created = await invoke(client, boot["runner"], "git.merge@1", {"branch": "b"}, runId=run["id"])
    assert created.status_code == 201, created.text
    invocation_id = created.json()["id"]

    # A gate requested after the call suspends it: it stays pending.
    gate = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": boot["adminId"], "gate": True},
        headers=auth(boot["admin"]),
    )
    assert gate.status_code == 201
    assert (await claim(client, boot["runner"])).status_code == 204
    assert status_of(sync_engine, invocation_id)[0] == "pending"

    # The run ends: its call has no basis any more.
    failed = await client.post(
        f"/api/v1/runs/{run['id']}:fail",
        json={"failureReason": "gave up"},
        headers=auth(boot["runner"]),
    )
    assert failed.status_code == 200, failed.text
    assert (await claim(client, boot["runner"])).status_code == 204
    status, error = status_of(sync_engine, invocation_id)
    assert status == "cancelled"
    assert error is not None and error["message"] == "run_not_active"


async def test_claim_can_name_the_invocation(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await published(client, boot["admin"])
    first = (await invoke(client, boot["caller"], "repo.search", {"query": "a"})).json()
    second = (await invoke(client, boot["caller"], "repo.search", {"query": "b"})).json()

    response = await client.post(
        "/api/v1/skill-invocations:claim",
        json={
            "protocols": ["local"],
            "localEntrypoints": ["cp_skills.search:run"],
            "invocationId": second["id"],
        },
        headers=auth(boot["executor"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["invocation"]["id"] == second["id"]
    assert (await claim(client, boot["executor"])).json()["invocation"]["id"] == first["id"]


# --- cancellation ---------------------------------------------------------------------


async def test_cancel_rights_and_states(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, boot["admin"])
    created = (await invoke(client, boot["caller"], "repo.search", {"query": "x"})).json()
    url = f"/api/v1/skill-invocations/{created['id']}:cancel"

    # Neither the executor nor another caller may stop somebody else's call.
    _, stranger = await create_agent_with_key(
        client, boot["admin"], name="stranger", permissions=CALLER
    )
    for key in (boot["executor"], stranger):
        response = await client.post(url, json={}, headers=auth(key))
        assert response.status_code in (403, 404), response.text
    assert status_of(sync_engine, created["id"])[0] == "pending"

    response = await client.post(url, json={"reason": "not needed"}, headers=auth(boot["caller"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["error"]["code"] == "cancelled"
    assert body["error"]["details"]["wasRunning"] is False
    # Stopped on a principal's request, not by core: the audit tells the two apart.
    assert body["error"]["details"]["initiator"] == "principal"
    assert body["error"]["details"]["cancelledBy"] is not None
    with sync_engine.connect() as conn:
        payload = conn.execute(
            text(
                "SELECT payload FROM events WHERE event_type = 'skill.invocation_cancelled' "
                "AND entity_id = :id"
            ),
            {"id": created["id"]},
        ).scalar_one()
    assert (payload["initiator"], payload["cancelledBy"]) == (
        "principal",
        body["error"]["details"]["cancelledBy"],
    )
    again = await client.post(url, json={}, headers=auth(boot["caller"]))
    assert again.status_code == 200
    assert again.json()["error"]["message"] == "not needed"
    assert (await claim(client, boot["executor"])).status_code == 204
    assert "skill.invocation_cancelled" in event_types(sync_engine, created["id"])

    # A tenant administrator may cancel any call; a finished one is 409.
    other = (await invoke(client, boot["caller"], "repo.search", {"query": "y"})).json()
    lease = (await claim(client, boot["executor"])).json()["invocation"]
    await client.post(
        f"/api/v1/skill-invocations/{other['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"hits": 1}},
        headers=auth(boot["executor"]),
    )
    response = await client.post(
        f"/api/v1/skill-invocations/{other['id']}:cancel", json={}, headers=auth(boot["admin"])
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invocation_terminal"


async def test_cancelling_a_running_call_fences_its_executor(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, boot["admin"])
    created = (await invoke(client, boot["caller"], "repo.search", {"query": "x"})).json()
    lease = (await claim(client, boot["executor"])).json()["invocation"]

    response = await client.post(
        f"/api/v1/skill-invocations/{created['id']}:cancel",
        json={"reason": "stop"},
        headers=auth(boot["admin"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["error"]["details"]["wasRunning"] is True

    for action, body in (
        ("heartbeat", {}),
        ("complete", {"output": {"hits": 1}}),
        ("fail", {"error": {"code": "x"}}),
    ):
        response = await client.post(
            f"/api/v1/skill-invocations/{created['id']}:{action}",
            json={"fencingToken": lease["fencingToken"], **body},
            headers=auth(boot["executor"]),
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "stale_invocation_lease"
    assert status_of(sync_engine, created["id"])[0] == "cancelled"


# --- review 2: no retried external write without idempotency ---------------------------


async def test_external_write_without_idempotency_is_attempted_once(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    admin = boot["admin"]
    retried = {"retryPolicy": {"maxAttempts": 3, "backoffSeconds": 0}}
    response = await publish(
        client, admin, "crm.update", side_effects="external_write", risk_level="high", **retried
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_skill_contract"
    assert error["details"]["field"] == "retryPolicy.maxAttempts"

    for version, overrides in (
        ("1", {"idempotency": "required", **retried}),
        ("2", {"idempotency": "natural", **retried}),
        ("3", {"retryPolicy": {"maxAttempts": 1, "backoffSeconds": 0}}),
    ):
        response = await publish(
            client,
            admin,
            "crm.update",
            version=version,
            side_effects="external_write",
            risk_level="high",
            **overrides,
        )
        assert response.status_code == 201, response.text
    # Reads and pure computations may retry freely.
    response = await publish(client, admin, "repo.search", side_effects="external_read", **retried)
    assert response.status_code == 201, response.text


# --- review 3: the executor sees the contract, not the catalog config -------------------


async def test_claim_does_not_reveal_the_catalog_config(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": "repo.search",
            "version": "1.0.0",
            "sideEffects": "none",
            "riskLevel": "low",
            "config": {"internalUrl": "https://internal.example/search"},
            "contract": {
                "inputs": {"type": "object"},
                "outputs": {"type": "object"},
                "implementation": {"protocol": "local", "entrypoint": "cp_skills.search:run"},
            },
        },
        headers=auth(boot["admin"]),
    )
    assert response.status_code == 201, response.text
    await invoke(client, boot["caller"], "repo.search", {})

    claimed = (await claim(client, boot["executor"])).json()
    skill = claimed["skill"]
    assert "config" not in skill
    assert set(skill) == {
        "id",
        "name",
        "version",
        "protocol",
        "sideEffects",
        "riskLevel",
        "contract",
    }
    assert skill["contract"]["implementation"]["entrypoint"] == "cp_skills.search:run"


# --- remote endpoints: the executor's allow-lists at :claim (amendment M2.2, D) ------


async def remote_claim(client: httpx.AsyncClient, key: str, **declaration: Any) -> httpx.Response:
    return await client.post(
        "/api/v1/skill-invocations:claim",
        json={"protocols": ["http", "mcp"], **declaration},
        headers=auth(key),
    )


async def drain(client: httpx.AsyncClient, key: str, **declaration: Any) -> set[str]:
    names: set[str] = set()
    while True:
        response = await remote_claim(client, key, **declaration)
        if response.status_code == 204:
            return names
        assert response.status_code == 200, response.text
        names.add(response.json()["skill"]["name"])


async def test_claim_hands_out_only_admitted_endpoints_and_audiences(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    remote = {
        "open": {"protocol": "http", "endpoint": "https://skills.test/open"},
        "secured": {
            "protocol": "http",
            "endpoint": "https://skills.test/secured",
            "auth": {"audience": "skill-service"},
        },
        "core-token": {
            "protocol": "http",
            "endpoint": "https://skills.test/core",
            "auth": {"audience": "control-plane"},
        },
        "loopback": {"protocol": "http", "endpoint": "http://127.0.0.1:9000/admin"},
        "lookalike": {"protocol": "http", "endpoint": "https://skills.test.evil.test/x"},
        "tools": {"protocol": "mcp", "endpoint": "stdio:git", "entrypoint": "merge"},
        "remote-tools": {"protocol": "mcp", "endpoint": "https://mcp.test", "entrypoint": "x"},
    }
    for name, implementation in remote.items():
        await published(client, boot["admin"], f"remote.{name}", implementation=implementation)
        response = await invoke(client, boot["caller"], f"remote.{name}", {"query": "q"})
        assert response.status_code == 201, response.text

    # A remote protocol declared without endpoints receives nothing.
    assert (await remote_claim(client, boot["executor"])).status_code == 204

    assert await drain(
        client,
        boot["executor"],
        httpOrigins=["HTTPS://Skills.Test/"],
        mcpEndpoints=["stdio:git", "https://mcp.test"],
    ) == {"remote.open", "remote.tools", "remote.remote-tools"}
    # A token-bearing call only to an executor that issues that audience.
    assert await drain(
        client,
        boot["executor"],
        httpOrigins=["https://skills.test"],
        audiences=["skill-service"],
    ) == {"remote.secured"}
    # The rest matches no declared origin: never handed to this executor.
    assert (
        await drain(
            client, boot["executor"], httpOrigins=["https://skills.test"], audiences=["other"]
        )
        == set()
    )


@pytest.mark.parametrize(
    "declaration",
    [
        {"httpOrigins": ["https://skills.test/api"]},
        {"httpOrigins": ["ftp://skills.test"]},
        {"mcpEndpoints": ["stdio:bad name"]},
        {"httpOrigins": ["stdio:git"]},
    ],
)
async def test_claim_rejects_what_is_not_an_origin(
    client: httpx.AsyncClient, boot: dict[str, Any], declaration: dict[str, Any]
) -> None:
    response = await remote_claim(client, boot["executor"], **declaration)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_executor_endpoint"
