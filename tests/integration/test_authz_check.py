"""May I do X on Y: ``POST /authz:check`` agrees with the endpoint.

CP-ADR-0055, amendment of 2026-09-29.

For every action of R011 (the console's controls) and a matrix of callers —
admin, a tenant-wide grant, a grant or role in one workspace, no right, one's
own approval — the answer of the check is compared with what the real
endpoint then does: allowed ⇔ 200, refused ⇔ 403/404 with the same code.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from platform_auth import ObjectPage, PolicyDecision
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.api.dependencies import get_auth_context
from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE as LIFECYCLE
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
from tests.integration.test_agent_registry import _link, coder_spec
from tests.integration.test_agent_registry import _publish as publish_agent
from tests.integration.test_agent_registry import _tenant as registry_tenant
from tests.integration.test_approval_outcomes import _review_setup
from tests.integration.test_approval_preconditions import CI_GREEN, _observe, _with_preconditions
from tests.integration.test_process_instances import GOAL, _publish
from tests.integration.test_process_instances import _setup as process_setup

DECIDER = ["approvals.read", "approvals.decide", "tasks.read"]


async def check(
    client: httpx.AsyncClient, key: str | None, action: str, resource_type: str, resource_id: str
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/authz:check",
        json={
            "checks": [{"action": action, "resourceType": resource_type, "resourceId": resource_id}]
        },
        headers=auth(key) if key else {},
    )
    assert response.status_code == 200, response.text
    [result] = response.json()["results"]
    assert (result["action"], result["resourceType"], result["resourceId"]) == (
        action,
        resource_type,
        resource_id,
    )
    return dict(result)


async def agree(
    client: httpx.AsyncClient,
    key: str | None,
    action: str,
    resource_type: str,
    resource_id: str,
    call: Callable[[], Awaitable[httpx.Response]],
    refusals: tuple[int, ...] = (403, 404),
) -> bool:
    """Ask, then do: the answer must be what the endpoint did."""
    verdict = await check(client, key, action, resource_type, resource_id)
    response = await call()
    if verdict["allowed"]:
        assert verdict["reason"] is None
        assert response.status_code == 200, (action, response.text)
    else:
        assert response.status_code in refusals, (action, response.text)
        error = response.json()["error"]
        assert verdict["reason"]["code"] == error["code"], (action, verdict, error)
        assert verdict["reason"]["details"] == error.get("details", {}), (action, verdict, error)
    return bool(verdict["allowed"])


def post(
    client: httpx.AsyncClient, key: str | None, path: str, body: dict[str, Any] | None = None
) -> Callable[[], Awaitable[httpx.Response]]:
    async def call() -> httpx.Response:
        headers = {**(auth(key) if key else {}), "Idempotency-Key": str(uuid.uuid4())}
        return await client.post(path, json=body or {}, headers=headers)

    return call


# --- approvals: role in the workspace, admin, own approval ------------------------------


async def test_approve_and_reject_agree_with_the_endpoint(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin = boot["apiKey"]["key"]
    here = await create_workspace(client, admin, "here")
    there = await create_workspace(client, admin, "there")
    role = await create_role(client, admin, "approver")

    async def person(
        name: str, permissions: list[str], scope: str | None, *, holds_role: bool = True
    ) -> dict[str, Any]:
        principal, key = await create_agent_with_key(
            client, admin, name=name, permissions=permissions, kind="human"
        )
        if holds_role:
            await assign_role(client, admin, principal["id"], role["id"], workspace_id=scope)
        return {"id": principal["id"], "key": key}

    initiator = await person("initiator", DECIDER, None)
    callers = {
        # (key, expected): expected pins the matrix so a regression in both
        # the check and the endpoint does not pass unnoticed.
        "tenant role": (await person("tenant-role", DECIDER, None), True),
        "workspace role": (await person("ws-role", DECIDER, here["id"]), True),
        "role in another workspace": (await person("elsewhere", DECIDER, there["id"]), False),
        "no permission": (await person("no-perm", ["approvals.read"], None), False),
        "no role": (await person("no-role", DECIDER, None, holds_role=False), False),
        "own approval": (initiator, False),
    }
    reasons = {}
    for name, (caller, expected) in callers.items():
        for verb in ("approve", "reject"):
            task = await create_task(client, admin, workspaceId=here["id"])
            created = await client.post(
                "/api/v1/approvals",
                json={
                    "task": task["id"],
                    "requiredRoleId": role["id"],
                    "excludedPrincipals": [initiator["id"]],
                },
                headers=auth(admin),
            )
            assert created.status_code == 201, created.text
            approval = created.json()["id"]
            path = f"/api/v1/approvals/{approval}:{verb}"
            allowed = await agree(
                client, caller["key"], verb, "approval", approval, post(client, caller["key"], path)
            )
            assert allowed is expected, (name, verb)
            if not allowed:
                reasons[name] = (await check(client, caller["key"], verb, "approval", approval))[
                    "reason"
                ]["code"]

    assert reasons == {
        "role in another workspace": "not_eligible",
        "no permission": "permission_denied",
        "no role": "not_eligible",
        "own approval": "separation_of_duties_violation",
    }

    # Admin holds every permission but not the role: eligibility is not a permission.
    task = await create_task(client, admin, workspaceId=here["id"])
    created = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "requiredRoleId": role["id"]},
        headers=auth(admin),
    )
    approval = created.json()["id"]
    path = f"/api/v1/approvals/{approval}:approve"
    assert not await agree(
        client, admin, "approve", "approval", approval, post(client, admin, path)
    )
    await assign_role(client, admin, boot["adminPrincipal"]["id"], role["id"])
    assert await agree(client, admin, "approve", "approval", approval, post(client, admin, path))


async def _green_gate(
    client: httpx.AsyncClient, reviewer_permissions: list[str], admin_key: str | None = None
) -> dict[str, Any]:
    """A gate whose type's precondition reads ``$.spawnedBy``, and the CI is green."""
    s = await _review_setup(
        client,
        reviewer_permissions=reviewer_permissions,
        schema=_with_preconditions(CI_GREEN),
        admin_key=admin_key,
    )
    await _observe(
        client,
        s["admin_key"],
        conclusion="success",
        observed_at="2026-09-29T10:00:00+00:00",
        task=s["coding"]["id"],
    )
    return s


async def test_approve_reads_the_context_of_the_preconditions_as_the_endpoint_does(
    client: httpx.AsyncClient,
) -> None:
    """An approve reads what the preconditions reference as the decider: the
    check asks the same, so ``allowed`` never ends in 403. A reject reads none."""
    s = await _green_gate(client, ["approvals.read", "approvals.decide", "artifacts.read"])
    key, approval = s["reviewer_key"], s["approval"]["id"]
    path = f"/api/v1/approvals/{approval}"

    assert not await agree(
        client, key, "approve", "approval", approval, post(client, key, f"{path}:approve")
    )
    reason = (await check(client, key, "approve", "approval", approval))["reason"]
    assert reason["code"] == "permission_denied"
    assert await agree(
        client, key, "reject", "approval", approval, post(client, key, f"{path}:reject")
    )


# --- runs: holder, operator, admin, no permission ---------------------------------------


async def test_request_cancel_and_cancel_of_a_run_agree_with_the_endpoint(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    _, holder = await create_agent_with_key(client, admin, name="holder")
    session = await open_session(client, holder)
    _, operator = await create_agent_with_key(
        client, admin, name="operator", permissions=["claims.manage"]
    )
    _, claimer = await create_agent_with_key(
        client, admin, name="claimer", permissions=["tasks.claim"]
    )
    _, reader = await create_agent_with_key(
        client, admin, name="reader", permissions=["tasks.read"]
    )

    async def running() -> str:
        task = await create_task(client, admin)
        claim = (await claim_task(client, holder, task["id"], session["id"])).json()
        started = await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(holder),
        )
        assert started.status_code == 201, started.text
        return str(started.json()["id"])

    expected = {
        # caller: (request-cancel, cancel)
        "holder": (holder, True, True),
        "operator": (operator, True, True),
        "admin": (admin, True, True),
        "another claimer": (claimer, False, False),
        "no permission": (reader, False, False),
    }
    for name, (key, may_request, may_cancel) in expected.items():
        run = await running()
        path = f"/api/v1/runs/{run}"
        requested = await agree(
            client, key, "request-cancel", "run", run, post(client, key, f"{path}:request-cancel")
        )
        cancelled = await agree(
            client, key, "cancel", "run", run, post(client, key, f"{path}:cancel")
        )
        assert (requested, cancelled) == (may_request, may_cancel), name
        if name == "another claimer":
            verdict = await check(client, key, "cancel", "run", run)
            assert verdict["reason"]["code"] == "run_holder_mismatch"


# --- process instances ------------------------------------------------------------------


async def test_suspend_resume_cancel_of_an_instance_agree_with_the_endpoint(
    client: httpx.AsyncClient,
) -> None:
    s = await process_setup(client)
    admin = s["key"]
    await _publish(client, admin, "sample-goal", GOAL)
    _, operator = await create_agent_with_key(
        client, admin, name="operator", permissions=["processes.read", "processes.operate"]
    )
    _, reader = await create_agent_with_key(
        client, admin, name="reader", permissions=["processes.read"]
    )
    _, nobody = await create_agent_with_key(
        client, admin, name="nobody", permissions=["tasks.read"]
    )

    for name, key, expected in (
        ("admin", admin, True),
        ("operator", operator, True),
        ("reader", reader, False),
        ("no permission", nobody, False),
    ):
        created = await client.post(
            "/api/v1/process-instances",
            json={"process": "sample-goal", "key": f"goal-{name}"},
            headers=auth(admin),
        )
        assert created.status_code == 201, created.text
        instance = created.json()["id"]
        path = f"/api/v1/process-instances/{instance}"
        answers = [
            await agree(
                client,
                key,
                "suspend",
                "process_instance",
                instance,
                post(client, key, f"{path}:suspend", {"reason": "hold"}),
            ),
            await agree(
                client,
                key,
                "resume",
                "process_instance",
                instance,
                post(client, key, f"{path}:resume"),
            ),
            await agree(
                client,
                key,
                "cancel",
                "process_instance",
                instance,
                post(client, key, f"{path}:cancel", {"reason": "done", "compensate": False}),
            ),
        ]
        assert answers == [expected] * 3, name


# --- rules and agents -------------------------------------------------------------------


async def test_enable_and_disable_of_a_rule_agree_with_the_endpoint(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    here = await create_workspace(client, admin, "here")
    _, writer = await create_agent_with_key(
        client, admin, name="writer", permissions=["rules.read", "rules.write"]
    )
    _, reader = await create_agent_with_key(
        client, admin, name="reader", permissions=["rules.read"]
    )
    rules = [await _rule(client, admin, "tenant-level"), await _rule(client, admin, "in-ws", here)]
    for name, key, expected in (
        ("admin", admin, True),
        ("writer", writer, True),
        ("reader", reader, False),
    ):
        for rule in rules:
            for verb in ("disable", "enable"):
                path = f"/api/v1/rules/{rule}:{verb}"
                allowed = await agree(client, key, verb, "rule", rule, post(client, key, path))
                assert allowed is expected, (name, verb)


async def test_in_local_mode_a_role_in_one_workspace_grants_no_operation(
    client: httpx.AsyncClient,
) -> None:
    """Local mode reads the credential's flat permissions: a role held in one
    workspace adds nothing, a permission holds in every workspace — and the
    check says so for an instance and a rule of either workspace."""
    s = await process_setup(client)
    admin = s["key"]
    await _publish(client, admin, "sample-goal", GOAL)
    here = await create_workspace(client, admin, "here")
    there = await create_workspace(client, admin, "there")
    role = await create_role(client, admin, "operator")
    permissions = ["processes.read", "processes.operate", "rules.read", "rules.write"]

    async def person(name: str, granted: list[str]) -> str:
        principal, key = await create_agent_with_key(
            client, admin, name=name, permissions=granted, kind="human"
        )
        await assign_role(client, admin, principal["id"], role["id"], workspace_id=here["id"])
        return key

    callers = {
        "permission, role in here": (await person("granted", permissions), True),
        "role in here only": (await person("role-only", ["processes.read", "rules.read"]), False),
    }
    for name, (key, expected) in callers.items():
        for workspace in (here, there):
            created = await client.post(
                "/api/v1/process-instances",
                json={
                    "process": "sample-goal",
                    "key": f"goal-{name}-{workspace['id']}",
                    "workspaceId": workspace["id"],
                },
                headers=auth(admin),
            )
            assert created.status_code == 201, created.text
            instance = created.json()
            assert instance["workspaceId"] == workspace["id"]
            path = f"/api/v1/process-instances/{instance['id']}"
            suspended = await agree(
                client,
                key,
                "suspend",
                "process_instance",
                instance["id"],
                post(client, key, f"{path}:suspend", {"reason": "hold"}),
            )
            assert suspended is expected, (name, workspace["id"], "suspend")

            rule = await _rule(client, admin, f"r-{uuid.uuid4().hex[:8]}", workspace)
            disabled = await agree(
                client,
                key,
                "disable",
                "rule",
                rule,
                post(client, key, f"/api/v1/rules/{rule}:disable"),
            )
            assert disabled is expected, (name, workspace["id"], "disable")


async def test_state_and_replicas_of_an_agent_agree_with_the_endpoint(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    created = await client.post(
        "/api/v1/agents",
        json={
            "key": "sample-agent",
            "spec": {
                "displayName": "Sample",
                "identity": {"kind": "service", "permissions": ["tasks.read"]},
                "placement": "none",
            },
        },
        headers=auth(admin),
    )
    assert created.status_code == 201, created.text
    _, manager = await create_agent_with_key(
        client, admin, name="manager", permissions=["agents.read", "agents.manage"]
    )
    _, reader = await create_agent_with_key(
        client, admin, name="reader", permissions=["agents.read"]
    )
    for name, key, expected in (
        ("admin", admin, True),
        ("manager", manager, True),
        ("reader", reader, False),
    ):
        for body in ({"state": "stopped"}, {"replicas": 2}):

            async def call(key: str = key, body: dict[str, Any] = body) -> httpx.Response:
                return await client.patch(
                    "/api/v1/agents/sample-agent/state", json=body, headers=auth(key)
                )

            allowed = await agree(client, key, "update-state", "agent", "sample-agent", call)
            assert allowed is expected, name


async def _rule(
    client: httpx.AsyncClient, key: str, rule_key: str, workspace: dict[str, Any] | None = None
) -> str:
    response = await client.post(
        "/api/v1/task-types",
        json={"key": f"work-{rule_key}", "displayName": "W", "lifecycleSchema": LIFECYCLE},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = {
        "key": rule_key,
        "trigger": {"kind": "observation", "type": "sample.found"},
        "action": {
            "kind": "ensure_work",
            "taskType": f"work-{rule_key}",
            "dedupKeyTemplate": "sample:{{payload.id}}",
            "fields": {"title": "Sample"},
        },
    }
    if workspace is not None:
        body["workspaceId"] = workspace["id"]
    created = await client.post("/api/v1/rules", json=body, headers=auth(key))
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


# --- principals: enable and disable ------------------------------------------------------

# The principal gates also refuse for what the target is (a service, the
# registry's agent, the caller itself): 409/422 with the endpoint's own code.
PRINCIPAL_REFUSALS = (403, 404, 409, 422)


async def test_enable_and_disable_of_a_principal_agree_with_the_endpoint(
    client: httpx.AsyncClient,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    manage = ["principals.read", "principals.write"]
    manager, manager_key = await create_agent_with_key(
        client, admin, name="manager", permissions=[*manage, "tasks.read"]
    )
    _, reader_key = await create_agent_with_key(
        client, admin, name="reader", permissions=["principals.read"]
    )
    callers = {"admin": admin, "manager": manager_key, "reader": reader_key}

    async def principal(kind: str = "human", status: str = "active") -> str:
        created = await client.post(
            "/api/v1/principals",
            json={"kind": kind, "displayName": f"{kind}-{uuid.uuid4().hex[:6]}", "status": status},
            headers=auth(admin),
        )
        assert created.status_code == 201, created.text
        return str(created.json()["id"])

    async def with_key(permissions: list[str]) -> str:
        target = await principal()
        issued = await client.post(
            f"/api/v1/principals/{target}/api-keys",
            json={"permissions": permissions},
            headers=auth(admin),
        )
        assert issued.status_code == 201, issued.text
        return target

    async def disabled(target: str) -> str:
        response = await client.post(f"/api/v1/principals/{target}:disable", headers=auth(admin))
        assert response.status_code == 200, response.text
        return target

    async def verdict(caller: str, verb: str, target: str) -> tuple[bool, str | None]:
        key = callers[caller]
        path = f"/api/v1/principals/{target}:{verb}"
        allowed = await agree(
            client, key, verb, "principal", target, post(client, key, path), PRINCIPAL_REFUSALS
        )
        if allowed:
            return True, None
        # Ask again: the endpoint refused, so nothing changed under the check.
        reason = (await check(client, key, verb, "principal", target))["reason"]
        return False, reason["code"]

    async def run(
        verb: str, make: Callable[[], Awaitable[str]], expected: dict[str, tuple[bool, str | None]]
    ) -> None:
        """Each caller against a fresh target: an allowed call changes it."""
        for caller, want in expected.items():
            target = await make()
            assert await verdict(caller, verb, target) == want, (verb, caller)

    async def disabled_plain() -> str:
        return await disabled(await principal())

    async def disabled_wide_key() -> str:
        return await disabled(await with_key(["tasks.read", "tasks.write"]))

    async def disabled_admin_key() -> str:
        return await disabled(await with_key(["admin"]))

    async def service() -> str:
        return await principal(kind="service", status="disabled")

    no_right = (False, "permission_denied")
    # enable
    await run(
        "enable",
        disabled_plain,
        {"admin": (True, None), "manager": (True, None), "reader": no_right},
    )
    # A live key with tasks.write, which the manager lacks: the rule of issuing a key.
    await run(
        "enable",
        disabled_wide_key,
        {"admin": (True, None), "manager": (False, "permission_escalation"), "reader": no_right},
    )
    await run(
        "enable",
        disabled_admin_key,
        {"admin": (True, None), "manager": (False, "permission_escalation"), "reader": no_right},
    )
    await run(
        "enable",
        service,
        {
            "admin": (False, "principal_kind_not_enableable"),
            "manager": (False, "principal_kind_not_enableable"),
            "reader": no_right,
        },
    )
    # A repeat is 200 on the endpoint, so the check allows it too.
    await run("enable", principal, {"admin": (True, None), "manager": (True, None)})

    # disable
    await run(
        "disable", principal, {"admin": (True, None), "manager": (True, None), "reader": no_right}
    )

    async def admin_principal() -> str:
        return await with_key(["admin"])

    await run(
        "disable",
        admin_principal,
        {"admin": (True, None), "manager": (False, "permission_escalation"), "reader": no_right},
    )

    async def active_service() -> str:
        return await principal(kind="service")

    await run(
        "disable",
        active_service,
        {
            "admin": (False, "principal_kind_not_disableable"),
            "manager": (False, "principal_kind_not_disableable"),
        },
    )
    await run("disable", disabled_plain, {"admin": (True, None), "manager": (True, None)})

    async def the_manager() -> str:
        return str(manager["id"])

    await run("disable", the_manager, {"manager": (False, "cannot_disable_self")})

    async def missing() -> str:
        return str(uuid.uuid4())

    for verb in ("enable", "disable"):
        await run(verb, missing, {"admin": (False, "not_found"), "reader": no_right})


async def test_a_registry_agent_principal_agrees_with_the_endpoint(
    client: httpx.AsyncClient,
) -> None:
    admin, workspace = await registry_tenant(client)
    assert (await publish_agent(client, admin, coder_spec(workspace["id"]))).status_code == 201
    principal_id = (await _link(client, admin)).json()["principalId"]

    def call(verb: str) -> Callable[[], Awaitable[httpx.Response]]:
        return post(client, admin, f"/api/v1/principals/{principal_id}:{verb}")

    assert not await agree(
        client, admin, "disable", "principal", principal_id, call("disable"), PRINCIPAL_REFUSALS
    )
    reason = (await check(client, admin, "disable", "principal", principal_id))["reason"]
    assert reason["code"] == "use_agent_retire"

    retired = await client.post(
        "/api/v1/agents/coder:retire", json={"reason": "done"}, headers=auth(admin)
    )
    assert retired.status_code == 200, retired.text
    assert not await agree(
        client, admin, "enable", "principal", principal_id, call("enable"), PRINCIPAL_REFUSALS
    )
    reason = (await check(client, admin, "enable", "principal", principal_id))["reason"]
    assert reason["code"] == "use_agent_publish"
    assert reason["details"]["agentStatus"] == "retired"


# --- policy mode: a grant in one workspace, a purpose-bound credential ------------------


@dataclass
class OneWorkspace:
    """A PDP that grants ``rules.write`` on the tenant and on one workspace only,
    and refuses a requester the decision of their own approval."""

    grants: set[str]
    requested: set[str] = field(default_factory=set)

    async def check(
        self,
        ctx: Any,
        action: str,
        resource: Any,
        *,
        contextual: Any = (),
        on_behalf_of: Any = None,
        consistency: str = "default",
    ) -> PolicyDecision:
        if action == "approvals.decide":
            allowed = resource.key not in self.requested
        else:
            allowed = resource.key in self.grants
        return PolicyDecision(
            allowed=allowed,
            reason_code="allowed" if allowed else "denied_no_binding",
            decision_id="fixed",
            policy_version="1",
            model_version="1",
            source="online",
            consistency_token=None,
            evaluated_at=datetime.now(UTC),
            action=action,
            resource=resource.key,
        )

    async def list_objects(
        self, ctx: Any, action: str, resource_type: str, **kwargs: Any
    ) -> ObjectPage:
        return ObjectPage(objects=[], cursor=None, model_version="1")


@pytest.fixture
def restore(app: FastAPI) -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_auth_context, None)
    configure_authorizer(Authorizer(None, "local"))


async def test_in_policy_mode_a_grant_in_one_workspace_is_what_the_check_sees(
    client: httpx.AsyncClient, app: FastAPI, restore: None
) -> None:
    boot = await do_bootstrap(client)
    admin = boot["apiKey"]["key"]
    here = await create_workspace(client, admin, "here")
    there = await create_workspace(client, admin, "there")
    in_here = await _rule(client, admin, "in-here", here)
    in_there = await _rule(client, admin, "in-there", there)
    person, _ = await create_agent_with_key(
        client, admin, name="person", permissions=["tasks.read"], kind="human"
    )
    role = await create_role(client, admin, "approver")
    await assign_role(client, admin, person["id"], role["id"])
    task = await create_task(client, admin)
    approvals = []
    for _ in range(2):
        created = await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "requiredRoleId": role["id"]},
            headers=auth(admin),
        )
        approvals.append(created.json()["id"])
    own, foreign = approvals

    tenant_id = boot["tenant"]["id"]
    configure_authorizer(
        Authorizer(
            OneWorkspace(
                grants={f"tenant:{tenant_id}", f"workspace:{here['id']}"},
                requested={f"approval:{own}"},
            ),
            "policy",
        )
    )
    # The flat permissions say nothing: in policy mode the PDP decides.
    ctx = AuthContext(
        tenant_id=uuid.UUID(tenant_id),
        principal_id=uuid.UUID(person["id"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )
    app.dependency_overrides[get_auth_context] = lambda: ctx

    for rule, expected in ((in_here, True), (in_there, False)):
        for verb in ("disable", "enable"):
            path = f"/api/v1/rules/{rule}:{verb}"
            assert (
                await agree(client, None, verb, "rule", rule, post(client, None, path)) is expected
            )
    for approval, expected in ((own, False), (foreign, True)):
        path = f"/api/v1/approvals/{approval}:approve"
        allowed = await agree(
            client, None, "approve", "approval", approval, post(client, None, path)
        )
        assert allowed is expected

    # A credential bound to one decision decides that one and nothing else.
    # A real decision token never reaches the check (``decision_purpose``
    # refuses any request but its own decision, CP-ADR-0055 A3): the override
    # keeps the gate's own branch guarded.
    third = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "requiredRoleId": role["id"]},
        headers=auth(admin),
    )
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        **{**ctx.__dict__, "purpose_ref": f"approval:{foreign}"}
    )
    other = third.json()["id"]
    path = f"/api/v1/approvals/{other}:approve"
    assert not await agree(client, None, "approve", "approval", other, post(client, None, path))
    assert (await check(client, None, "approve", "approval", other))["reason"]["code"] == (
        "outside_purpose"
    )


async def test_in_policy_mode_approve_needs_tasks_read_on_the_referenced_task(
    client: httpx.AsyncClient, app: FastAPI, restore: None
) -> None:
    """``tasks.read`` on the tenant is not ``tasks.read`` on the task the
    preconditions read: without the latter the check refuses, as the endpoint."""
    boot = await do_bootstrap(client)
    s = await _green_gate(client, ["approvals.read", "approvals.decide"], boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]
    approval = s["approval"]["id"]
    pdp = OneWorkspace(grants={f"tenant:{tenant_id}", f"task:{s['review']['id']}"})
    configure_authorizer(Authorizer(pdp, "policy"))
    ctx = AuthContext(
        tenant_id=uuid.UUID(tenant_id),
        principal_id=uuid.UUID(s["reviewer"]["id"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )
    app.dependency_overrides[get_auth_context] = lambda: ctx
    path = f"/api/v1/approvals/{approval}:approve"

    assert not await agree(client, None, "approve", "approval", approval, post(client, None, path))
    reason = (await check(client, None, "approve", "approval", approval))["reason"]
    assert reason["code"] == "permission_denied"
    assert reason["details"]["resource"] == f"task:{s['coding']['id']}"

    pdp.grants.add(f"task:{s['coding']['id']}")
    assert await agree(client, None, "approve", "approval", approval, post(client, None, path))


# --- the request itself -----------------------------------------------------------------


async def test_a_batch_answers_in_order_and_writes_nothing(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    rule = await _rule(client, admin, "sample")
    _, reader = await create_agent_with_key(
        client, admin, name="reader", permissions=["rules.read"]
    )
    missing = str(uuid.uuid4())
    checks = [
        {"action": "disable", "resourceType": "rule", "resourceId": rule},
        {"action": "approve", "resourceType": "approval", "resourceId": missing},
        {"action": "update-state", "resourceType": "agent", "resourceId": "nobody"},
    ]
    with sync_engine.connect() as conn:
        events_before = conn.execute(text("SELECT count(*) FROM events")).scalar_one()

    as_admin = await client.post(
        "/api/v1/authz:check", json={"checks": checks}, headers=auth(admin)
    )
    assert as_admin.status_code == 200, as_admin.text
    results = as_admin.json()["results"]
    assert [(r["resourceType"], r["allowed"]) for r in results] == [
        ("rule", True),
        ("approval", False),
        ("agent", False),
    ]
    assert [r["reason"] and r["reason"]["code"] for r in results] == [
        None,
        "not_found",
        "not_found",
    ]
    as_reader = await client.post(
        "/api/v1/authz:check", json={"checks": checks}, headers=auth(reader)
    )
    # Without the permission the endpoint refuses before it looks the resource up.
    assert [r["reason"]["code"] for r in as_reader.json()["results"]] == ["permission_denied"] * 3

    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM events")).scalar_one() == events_before
    read = await client.get(f"/api/v1/rules/{rule}", headers=auth(admin))
    assert read.json()["status"] == "enabled"


async def test_a_malformed_batch_is_refused_whole(client: httpx.AsyncClient) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    item = {"action": "enable", "resourceType": "rule", "resourceId": str(uuid.uuid4())}

    too_many = await client.post(
        "/api/v1/authz:check", json={"checks": [item] * 101}, headers=auth(admin)
    )
    assert too_many.status_code == 400, too_many.text
    empty = await client.post("/api/v1/authz:check", json={"checks": []}, headers=auth(admin))
    assert empty.status_code == 400, empty.text

    not_an_id = await client.post(
        "/api/v1/authz:check",
        json={"checks": [item, {**item, "resourceId": "not-an-id"}]},
        headers=auth(admin),
    )
    assert not_an_id.status_code == 422, not_an_id.text
    assert not_an_id.json()["error"]["details"] == {"resourceIds": ["not-an-id"]}

    mismatched = await client.post(
        "/api/v1/authz:check",
        json={"checks": [item, {**item, "action": "approve"}]},
        headers=auth(admin),
    )
    assert mismatched.status_code == 422, mismatched.text
    error = mismatched.json()["error"]
    assert error["code"] == "unknown_action"
    assert error["details"]["unknown"] == [{"action": "approve", "resourceType": "rule"}]

    # A verb or type the schema does not know is not "unknown_action": the
    # schema refuses the whole batch (CP-ADR-0055 A3).
    for field_name, value in (("action", "archive"), ("resourceType", "task")):
        unknown = await client.post(
            "/api/v1/authz:check",
            json={"checks": [item, {**item, field_name: value}]},
            headers=auth(admin),
        )
        assert unknown.status_code == 400, unknown.text
        assert unknown.json()["error"]["code"] == "invalid_request"

    anonymous = await client.post("/api/v1/authz:check", json={"checks": [item]})
    assert anonymous.status_code == 401
