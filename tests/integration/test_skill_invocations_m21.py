"""M2.1: skill contract v1, immutable versions and skill_invocations (ADR-0056)."""

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.common import utcnow
from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap, register_skill

CALLER_PERMISSIONS = ["skills.invoke", "tasks.read", "tasks.write"]
EXECUTOR_PERMISSIONS = ["skills.execute", "sessions.open"]

INPUTS = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"],
    "additionalProperties": False,
}
OUTPUTS = {
    "type": "object",
    "properties": {"hits": {"type": "integer", "minimum": 0}},
    "required": ["hits"],
}


def contract(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "inputs": INPUTS,
        "outputs": OUTPUTS,
        "implementation": {"protocol": "local", "entrypoint": "cp_skills.search:run"},
    }
    body.update(overrides)
    return body


async def publish(
    client: httpx.AsyncClient,
    key: str,
    name: str = "repo.search",
    *,
    version: str = "1.0.0",
    side_effects: str = "none",
    risk_level: str = "low",
    **contract_overrides: Any,
) -> httpx.Response:
    return await client.post(
        "/api/v1/skills",
        json={
            "name": name,
            "version": version,
            "sideEffects": side_effects,
            "riskLevel": risk_level,
            "contract": contract(**contract_overrides),
        },
        headers=auth(key),
    )


async def published(client: httpx.AsyncClient, key: str, *args: Any, **kwargs: Any) -> dict:
    response = await publish(client, key, *args, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def invoke(
    client: httpx.AsyncClient, key: str, ref: str, inputs: dict[str, Any], **extra: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/skills/{ref}:invoke", json={"inputs": inputs, **extra}, headers=auth(key)
    )


async def claim(
    client: httpx.AsyncClient,
    key: str,
    *,
    protocols: list[str] | None = None,
    entrypoints: list[str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/api/v1/skill-invocations:claim",
        json={
            "protocols": protocols or ["local"],
            "localEntrypoints": ["cp_skills.search:run"] if entrypoints is None else entrypoints,
        },
        headers=auth(key),
    )


@pytest.fixture
async def actors(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    caller, caller_key = await create_agent_with_key(
        client, admin_key, name="caller", permissions=CALLER_PERMISSIONS
    )
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=EXECUTOR_PERMISSIONS
    )
    _, other_executor_key = await create_agent_with_key(
        client, admin_key, name="executor-2", permissions=EXECUTOR_PERMISSIONS
    )
    return {
        "admin": admin_key,
        "adminId": boot["adminPrincipal"]["id"],
        "caller": caller_key,
        "callerId": caller["id"],
        "executor": executor_key,
        "executor2": other_executor_key,
    }


def event_types(sync_engine: Engine, entity_id: str) -> list[str]:
    with sync_engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT event_type FROM events WHERE entity_id = :id ORDER BY sequence"),
                {"id": entity_id},
            ).scalars()
        )


# --- §1 contract at publication ---------------------------------------------


async def test_publication_normalizes_a_valid_contract(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    skill = await published(client, actors["admin"], costModel={"unit": "call", "estimate": 1})

    assert skill["protocol"] == "local"
    assert skill["sideEffects"] == "none"
    assert skill["riskLevel"] == "low"
    assert skill["inputSchema"] == INPUTS
    assert skill["outputSchema"] == OUTPUTS
    assert skill["contract"] == {
        "inputs": INPUTS,
        "outputs": OUTPUTS,
        "requiredPermissions": [],
        "preconditions": [],
        "postconditions": [],
        "timeoutSeconds": 60,
        "retryPolicy": {"maxAttempts": 1, "backoffSeconds": 0},
        "idempotency": "none",
        "costModel": {"unit": "call", "estimate": 1},
        "implementation": {
            "protocol": "local",
            "endpoint": None,
            "auth": None,
            "entrypoint": "cp_skills.search:run",
        },
    }

    # Readable by reference too, with the whole contract (cp_describe_skill).
    by_ref = await client.get("/api/v1/skills/repo.search@1.0.0", headers=auth(actors["caller"]))
    assert by_ref.status_code == 200, by_ref.text
    assert by_ref.json()["contract"] == skill["contract"]
    by_name = await client.get("/api/v1/skills/repo.search", headers=auth(actors["caller"]))
    assert by_name.json()["id"] == skill["id"]


@pytest.mark.parametrize(
    ("overrides", "code", "field"),
    [
        ({"inputs": {"type": "nope"}}, "invalid_json_schema", "inputs"),
        ({"outputs": {"$ref": "https://evil.example/schema"}}, "invalid_json_schema", "outputs"),
        (
            {"inputs": {"$schema": "http://json-schema.org/draft-07/schema#"}},
            "invalid_skill_contract",
            "inputs",
        ),
        ({"implementation": {"protocol": "opencode"}}, "invalid_skill_contract", None),
        (
            {"implementation": {"protocol": "local", "entrypoint": "not an entrypoint"}},
            "invalid_skill_contract",
            "implementation.entrypoint",
        ),
        (
            {"implementation": {"protocol": "http", "endpoint": "ftp://x"}},
            "invalid_skill_contract",
            "implementation.endpoint",
        ),
        (
            {
                "implementation": {
                    "protocol": "http",
                    "endpoint": "https://skills.example/run",
                    "auth": {"token": "hunter2"},
                }
            },
            "secret_material_rejected",
            None,
        ),
        ({"requiredPermissions": ["root.everything"]}, "invalid_skill_contract", None),
        ({"preconditions": [{"==": [1, 1]}]}, "unsupported_skill_condition", "preconditions"),
        ({"postconditions": [{"==": [1, 1]}]}, "unsupported_skill_condition", "postconditions"),
        ({"retryPolicy": {"maxAttempts": 0}}, "invalid_skill_contract", None),
        ({"timeoutSeconds": 0}, "invalid_skill_contract", "timeoutSeconds"),
        ({"idempotency": "sometimes"}, "invalid_skill_contract", "idempotency"),
        ({"costModel": {"estimate": -1}}, "invalid_skill_contract", "costModel"),
        ({"surprise": True}, "invalid_skill_contract", "contract"),
    ],
)
async def test_publication_rejects_an_invalid_contract(
    client: httpx.AsyncClient,
    actors: dict[str, Any],
    overrides: dict[str, Any],
    code: str,
    field: str | None,
) -> None:
    response = await publish(client, actors["admin"], **overrides)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == code
    if field is not None:
        assert error["details"]["field"] == field


async def test_contract_requires_policy_columns_and_a_consistent_protocol(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    response = await client.post(
        "/api/v1/skills",
        json={"name": "x", "contract": contract()},
        headers=auth(actors["admin"]),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["field"] == "sideEffects"

    response = await client.post(
        "/api/v1/skills",
        json={
            "name": "x",
            "protocol": "http",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": contract(),
        },
        headers=auth(actors["admin"]),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_skill_contract"


# --- §1 immutability --------------------------------------------------------


async def test_published_version_is_immutable(
    client: httpx.AsyncClient, actors: dict[str, Any], sync_engine: Engine
) -> None:
    admin = actors["admin"]
    skill = await published(client, admin)
    url = f"/api/v1/skills/{skill['id']}"

    for body in ({"config": {"x": 1}}, {"inputSchema": {"type": "object"}}):
        response = await client.patch(
            url, json=body, headers={**auth(admin), "If-Match": '"skill-1"'}
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "skill_version_immutable"

    # The catalog summary is not the contract.
    response = await client.patch(
        url, json={"description": "Search"}, headers={**auth(admin), "If-Match": '"skill-1"'}
    )
    assert response.status_code == 200, response.text

    response = await client.patch(
        url, json={"status": "deprecated"}, headers={**auth(admin), "If-Match": '"skill-2"'}
    )
    assert response.status_code == 200
    response = await client.patch(
        url, json={"status": "active"}, headers={**auth(admin), "If-Match": '"skill-3"'}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_status_transition"

    # The database holds the line even if the application does not.
    for statement in (
        "UPDATE skills SET contract = '{}'::jsonb WHERE id = :id",
        "UPDATE skills SET side_effects = 'external_write' WHERE id = :id",
        "UPDATE skills SET config = '{\"a\": 1}'::jsonb WHERE id = :id",
        "UPDATE skills SET status = 'active' WHERE id = :id",
        "DELETE FROM skills WHERE id = :id",
    ):
        with (
            pytest.raises(Exception, match=r"immutable|may only move"),
            sync_engine.begin() as conn,
        ):
            conn.execute(text(statement), {"id": skill["id"]})

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE skills SET status = 'disabled' WHERE id = :id"), {"id": skill["id"]}
        )


# --- invoke checks ----------------------------------------------------------


async def test_catalog_only_and_disabled_versions_are_not_invocable(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    admin, caller = actors["admin"], actors["caller"]
    # The pre-M2.1 shape: an http skill with no contract (BidOps catalog rows).
    await register_skill(client, admin, "bidops.fetch", protocol="http")
    await register_skill(client, admin, "legacy.agent", protocol="opencode")
    for ref, reason in (
        ("bidops.fetch", "no_contract"),
        ("legacy.agent@1.0.0", "no_contract"),
    ):
        response = await invoke(client, caller, ref, {})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "skill_not_invocable"
        assert response.json()["error"]["details"]["reason"] == reason

    skill = await published(client, admin)
    await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"status": "disabled"},
        headers={**auth(admin), "If-Match": '"skill-1"'},
    )
    response = await invoke(client, caller, "repo.search@1.0.0", {"query": "x"})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "disabled"

    assert (await invoke(client, caller, "nope@1.0.0", {})).status_code == 404


async def test_invalid_inputs_are_a_bad_request(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    await published(client, actors["admin"])
    response = await invoke(client, actors["caller"], "repo.search@1.0.0", {"query": 5})
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_skill_inputs"
    assert error["details"]["errors"][0]["path"] == "/query"

    response = await invoke(client, actors["caller"], "repo.search", {"other": "x"})
    assert response.status_code == 400


async def test_invoke_needs_the_right_and_the_declared_permissions(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    await published(client, actors["admin"])
    await published(
        client, actors["admin"], "repo.admin", requiredPermissions=["operations.manage"]
    )
    # The executor may run skills, not ask for them.
    response = await invoke(client, actors["executor"], "repo.search", {"query": "x"})
    assert response.status_code == 403

    response = await invoke(client, actors["caller"], "repo.admin", {"query": "x"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "skill_permission_denied"
    assert response.json()["error"]["details"]["missing"] == ["operations.manage"]

    # An admin holds every permission.
    response = await invoke(client, actors["admin"], "repo.admin", {"query": "x"})
    assert response.status_code == 201, response.text


async def test_bare_name_resolves_to_the_newest_active_version(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    await published(client, actors["admin"], version="1.0.0")
    v2 = await published(client, actors["admin"], version="2.0.0")
    response = await invoke(client, actors["caller"], "repo.search", {"query": "x"})
    assert response.status_code == 201
    assert response.json()["skillId"] == v2["id"]
    assert response.json()["skill"] == {"name": "repo.search", "version": "2.0.0"}
    assert response.json()["requestedBy"] == {"kind": "principal", "ref": actors["callerId"]}


async def test_idempotency_key_returns_the_existing_invocation(
    client: httpx.AsyncClient, actors: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, actors["admin"], idempotency="required")
    caller = actors["caller"]

    response = await invoke(client, caller, "repo.search", {"query": "x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"

    first = await invoke(client, caller, "repo.search", {"query": "x"}, idempotencyKey="k-1")
    assert first.status_code == 201, first.text
    again = await invoke(client, caller, "repo.search", {"query": "x"}, idempotencyKey="k-1")
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]

    other = await invoke(client, caller, "repo.search", {"query": "y"}, idempotencyKey="k-1")
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "idempotency_key_reuse"

    # Another principal cannot pick up someone else's call by guessing its key.
    stranger = await invoke(
        client, actors["admin"], "repo.search", {"query": "x"}, idempotencyKey="k-1"
    )
    assert stranger.status_code == 409
    assert stranger.json()["error"]["details"] == {}

    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar_one()
    assert count == 1
    assert event_types(sync_engine, first.json()["id"]) == ["skill.invocation_requested"]


# --- §4 side effects ----------------------------------------------------------


async def test_external_write_needs_an_approved_gate(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    admin, caller = actors["admin"], actors["caller"]
    await published(client, admin, "crm.update", side_effects="external_write", risk_level="high")
    task = await create_task(client, admin, title="Update CRM")

    response = await invoke(client, caller, "crm.update", {"query": "x"}, taskId=task["id"])
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "skill_side_effect_not_authorized"

    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "assignedPrincipalId": actors["adminId"], "gate": True},
            headers=auth(admin),
        )
    ).json()
    pending = await invoke(
        client, caller, "crm.update", {"query": "x"}, taskId=task["id"], approvalId=approval["id"]
    )
    assert pending.status_code == 403

    decided = await client.post(f"/api/v1/approvals/{approval['id']}:approve", headers=auth(admin))
    assert decided.status_code == 200

    # The approval gates THIS task, not any Work item.
    elsewhere = await create_task(client, admin, title="Other")
    response = await invoke(
        client,
        caller,
        "crm.update",
        {"query": "x"},
        taskId=elsewhere["id"],
        approvalId=approval["id"],
    )
    assert response.status_code == 403

    response = await invoke(
        client, caller, "crm.update", {"query": "x"}, taskId=task["id"], approvalId=approval["id"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["authorizationBasis"]["approvalId"] == approval["id"]


# --- executor: lease, fencing, results ----------------------------------------


async def test_claim_filters_by_what_the_executor_can_run(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    await published(client, actors["admin"])
    await invoke(client, actors["caller"], "repo.search", {"query": "x"})

    assert (await claim(client, actors["caller"])).status_code == 403
    assert (await claim(client, actors["executor"], protocols=["http"])).status_code == 204
    assert (await claim(client, actors["executor"], entrypoints=["other:run"])).status_code == 204
    response = await claim(client, actors["executor"], protocols=["custom"])
    assert response.status_code == 422

    claimed = await claim(client, actors["executor"])
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["invocation"]["status"] == "running"
    assert body["invocation"]["attempt"] == 1
    assert body["invocation"]["fencingToken"] == 1
    assert body["skill"]["contract"]["implementation"]["entrypoint"] == "cp_skills.search:run"
    # Nothing else is left to take.
    assert (await claim(client, actors["executor2"])).status_code == 204


async def test_fencing_rejects_a_foreign_or_stale_executor(
    client: httpx.AsyncClient, actors: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, actors["admin"], retryPolicy={"maxAttempts": 2})
    created = (await invoke(client, actors["caller"], "repo.search", {"query": "x"})).json()
    first = (await claim(client, actors["executor"])).json()["invocation"]
    url = f"/api/v1/skill-invocations/{created['id']}"

    foreign = await client.post(
        f"{url}:complete",
        json={"fencingToken": first["fencingToken"], "output": {"hits": 1}},
        headers=auth(actors["executor2"]),
    )
    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "stale_invocation_lease"

    wrong_token = await client.post(
        f"{url}:complete",
        json={"fencingToken": 99, "output": {"hits": 1}},
        headers=auth(actors["executor"]),
    )
    assert wrong_token.status_code == 409

    heartbeat = await client.post(
        f"{url}:heartbeat",
        json={"fencingToken": first["fencingToken"], "leaseSeconds": 30},
        headers=auth(actors["executor"]),
    )
    assert heartbeat.status_code == 200, heartbeat.text

    # The lease dies; another executor takes the call over with a new token.
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE skill_invocations SET lease_expires_at = :past WHERE id = :id"),
            {"past": utcnow() - timedelta(seconds=5), "id": created["id"]},
        )
    second = (await claim(client, actors["executor2"])).json()["invocation"]
    assert second["id"] == created["id"]
    assert second["attempt"] == 2
    assert second["fencingToken"] == 2

    zombie = await client.post(
        f"{url}:complete",
        json={"fencingToken": first["fencingToken"], "output": {"hits": 1}},
        headers=auth(actors["executor"]),
    )
    assert zombie.status_code == 409

    done = await client.post(
        f"{url}:complete",
        json={"fencingToken": second["fencingToken"], "output": {"hits": 3}},
        headers=auth(actors["executor2"]),
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "succeeded"
    assert done.json()["output"] == {"hits": 3}
    types = event_types(sync_engine, created["id"])
    assert "skill.invocation_retry_scheduled" in types
    assert types[-1] == "skill.invocation_succeeded"


async def test_complete_revalidates_the_output(
    client: httpx.AsyncClient, actors: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, actors["admin"], retryPolicy={"maxAttempts": 3})
    created = (await invoke(client, actors["caller"], "repo.search", {"query": "x"})).json()
    lease = (await claim(client, actors["executor"])).json()["invocation"]

    response = await client.post(
        f"/api/v1/skill-invocations/{created['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"hits": -1}},
        headers=auth(actors["executor"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["output"] is None
    assert body["error"]["code"] == "output_contract_violation"
    assert body["error"]["retryable"] is False
    assert event_types(sync_engine, created["id"])[-1] == "skill.invocation_failed"

    # The caller sees the same verdict.
    seen = await client.get(
        f"/api/v1/skill-invocations/{created['id']}", headers=auth(actors["caller"])
    )
    assert seen.json()["status"] == "failed"


async def test_retryable_failure_retries_until_attempts_are_exhausted(
    client: httpx.AsyncClient, actors: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, actors["admin"], retryPolicy={"maxAttempts": 2, "backoffSeconds": 0})
    created = (await invoke(client, actors["caller"], "repo.search", {"query": "x"})).json()
    url = f"/api/v1/skill-invocations/{created['id']}"

    lease = (await claim(client, actors["executor"])).json()["invocation"]
    response = await client.post(
        f"{url}:fail",
        json={
            "fencingToken": lease["fencingToken"],
            "error": {"code": "upstream_unavailable", "message": "503", "retryable": True},
        },
        headers=auth(actors["executor"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["attempt"] == 1
    assert response.json()["error"]["code"] == "upstream_unavailable"

    lease = (await claim(client, actors["executor"])).json()["invocation"]
    assert lease["attempt"] == 2
    response = await client.post(
        f"{url}:fail",
        json={
            "fencingToken": lease["fencingToken"],
            "error": {"code": "upstream_unavailable", "retryable": True},
        },
        headers=auth(actors["executor"]),
    )
    assert response.json()["status"] == "failed"
    assert response.json()["attempt"] == 2
    assert event_types(sync_engine, created["id"])[-1] == "skill.invocation_failed"

    # A non-retryable failure is final at once.
    created = (await invoke(client, actors["caller"], "repo.search", {"query": "y"})).json()
    lease = (await claim(client, actors["executor"])).json()["invocation"]
    response = await client.post(
        f"/api/v1/skill-invocations/{created['id']}:fail",
        json={"fencingToken": lease["fencingToken"], "error": {"code": "bad_input"}},
        headers=auth(actors["executor"]),
    )
    assert response.json()["status"] == "failed"
    assert response.json()["attempt"] == 1


async def test_backoff_delays_the_retry(client: httpx.AsyncClient, actors: dict[str, Any]) -> None:
    await published(client, actors["admin"], retryPolicy={"maxAttempts": 2, "backoffSeconds": 600})
    created = (await invoke(client, actors["caller"], "repo.search", {"query": "x"})).json()
    lease = (await claim(client, actors["executor"])).json()["invocation"]
    await client.post(
        f"/api/v1/skill-invocations/{created['id']}:fail",
        json={"fencingToken": lease["fencingToken"], "error": {"code": "x", "retryable": True}},
        headers=auth(actors["executor"]),
    )
    assert (await claim(client, actors["executor"])).status_code == 204


async def test_success_on_a_task_leaves_a_skill_result_artifact(
    client: httpx.AsyncClient, actors: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, actors["admin"])
    task = await create_task(client, actors["admin"], title="Search")
    created = await invoke(
        client, actors["caller"], "repo.search", {"query": "x"}, taskId=task["publicId"]
    )
    assert created.status_code == 201, created.text
    invocation = created.json()
    assert invocation["taskId"] == task["id"]

    lease = (await claim(client, actors["executor"])).json()["invocation"]
    done = await client.post(
        f"/api/v1/skill-invocations/{invocation['id']}:complete",
        json={
            "fencingToken": lease["fencingToken"],
            "output": {"hits": 2},
            "cost": {"unit": "call", "amount": 1},
        },
        headers=auth(actors["executor"]),
    )
    assert done.status_code == 200, done.text
    artifact_id = done.json()["artifactId"]
    assert artifact_id is not None

    artifact = await client.get(f"/api/v1/artifacts/{artifact_id}", headers=auth(actors["admin"]))
    assert artifact.status_code == 200, artifact.text
    body = artifact.json()
    assert body["type"] == "skill_result"
    assert body["taskId"] == task["id"]
    assert body["createdByPrincipalId"] == actors["callerId"]
    assert body["content"]["output"] == {"hits": 2}
    assert event_types(sync_engine, invocation["id"]) == [
        "skill.invocation_requested",
        "skill.invocation_claimed",
        "skill.invocation_succeeded",
    ]

    # Finished is final: nothing more to report under the old lease.
    again = await client.post(
        f"/api/v1/skill-invocations/{invocation['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"hits": 2}},
        headers=auth(actors["executor"]),
    )
    assert again.status_code == 409


async def test_invocation_is_visible_to_its_parties_only(
    client: httpx.AsyncClient, actors: dict[str, Any]
) -> None:
    await published(client, actors["admin"])
    created = (await invoke(client, actors["caller"], "repo.search", {"query": "x"})).json()
    url = f"/api/v1/skill-invocations/{created['id']}"
    _, bystander = await create_agent_with_key(
        client, actors["admin"], name="bystander", permissions=["skills.invoke"]
    )
    assert (await client.get(url, headers=auth(actors["caller"]))).status_code == 200
    assert (await client.get(url, headers=auth(actors["executor"]))).status_code == 200
    assert (await client.get(url, headers=auth(bystander))).status_code == 404


async def test_worker_returns_an_expired_lease_to_the_queue(
    client: httpx.AsyncClient, actors: dict[str, Any], sync_engine: Engine, settings: Settings
) -> None:
    await published(client, actors["admin"])  # maxAttempts 1
    created = (await invoke(client, actors["caller"], "repo.search", {"query": "x"})).json()
    await claim(client, actors["executor"])
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE skill_invocations SET lease_expires_at = :past WHERE id = :id"),
            {"past": utcnow() - timedelta(seconds=5), "id": created["id"]},
        )
    worker = Worker(settings)
    try:
        stats = await worker.run_once()
    finally:
        await worker.engine.dispose()
    assert stats["skill_leases_expired"] == 1

    seen = await client.get(
        f"/api/v1/skill-invocations/{created['id']}", headers=auth(actors["caller"])
    )
    # One attempt only: the dead lease consumed it.
    assert seen.json()["status"] == "failed"
    assert seen.json()["error"]["code"] == "lease_expired"
