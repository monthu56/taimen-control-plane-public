"""SDK and MCP surface of the skill runtime (ADR-0056) against a real Control Plane."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

import control_plane_mcp.server as mcp_server
from control_plane_client import ControlPlaneClient, ControlPlaneError
from tests.helpers import create_agent_with_key, do_bootstrap, open_session

Make = Callable[[str], ControlPlaneClient]

CONTRACT = {
    "inputs": {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    },
    "outputs": {
        "type": "object",
        "properties": {"double": {"type": "integer"}},
        "required": ["double"],
    },
    "retryPolicy": {"maxAttempts": 2, "backoffSeconds": 0},
    "implementation": {"protocol": "local", "entrypoint": "cp_skills.math:double"},
}


async def _setup(client: httpx.AsyncClient) -> tuple[str, str, str]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, caller_key = await create_agent_with_key(
        client, admin_key, name="caller", permissions=["skills.invoke", "tasks.read"]
    )
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=["skills.execute"]
    )
    return admin_key, caller_key, executor_key


async def test_sdk_invocation_cycle(client: httpx.AsyncClient, sdk: Make) -> None:
    admin_key, caller_key, executor_key = await _setup(client)
    async with sdk(admin_key) as admin, sdk(caller_key) as caller, sdk(executor_key) as executor:
        skill = await admin.register_skill(
            name="math.double", side_effects="none", risk_level="low", contract=CONTRACT
        )
        assert skill["protocol"] == "local"
        described = await caller.describe_skill("math.double@1.0.0")
        assert described["contract"]["retryPolicy"] == {"maxAttempts": 2, "backoffSeconds": 0}

        with pytest.raises(ControlPlaneError) as bad:
            await caller.invoke_skill("math.double", inputs={"n": "two"})
        assert bad.value.code == "invalid_skill_inputs"
        assert bad.value.status == 400

        created = await caller.invoke_skill("math.double", inputs={"n": 2}, idempotency_key="a")
        again = await caller.invoke_skill("math.double", inputs={"n": 2}, idempotency_key="a")
        assert again["id"] == created["id"]

        assert await executor.claim_skill_invocation(protocols=["http"]) is None
        claimed = await executor.claim_skill_invocation(
            protocols=["local"], local_entrypoints=["cp_skills.math:double"]
        )
        assert claimed is not None
        lease = claimed["invocation"]
        token = lease["fencingToken"]
        assert claimed["skill"]["contract"]["implementation"]["entrypoint"] == (
            "cp_skills.math:double"
        )

        await executor.heartbeat_skill_invocation(lease["id"], fencing_token=token)
        retried = await executor.fail_skill_invocation(
            lease["id"], fencing_token=token, code="flaky", retryable=True
        )
        assert retried["status"] == "pending"

        claimed = await executor.claim_skill_invocation(
            protocols=["local"], local_entrypoints=["cp_skills.math:double"]
        )
        assert claimed is not None
        with pytest.raises(ControlPlaneError) as stale:
            await executor.complete_skill_invocation(
                lease["id"], fencing_token=token, output={"double": 4}
            )
        assert stale.value.code == "stale_invocation_lease"

        done = await executor.complete_skill_invocation(
            lease["id"],
            fencing_token=claimed["invocation"]["fencingToken"],
            output={"double": 4},
        )
        assert done["status"] == "succeeded"
        seen = await caller.wait_skill_invocation(created["id"], timeout_seconds=0)
        assert seen["output"] == {"double": 4}
        assert (await caller.get_skill_invocation(created["id"]))["attempt"] == 2


@pytest.fixture
async def _mcp_state(app) -> AsyncIterator[None]:
    state = mcp_server.STATE
    for name in ("client", "session_id", "claim_id", "fencing_token", "run_id", "task_ref"):
        setattr(state, name, None)
    state.heartbeats = None
    state.session_lock = None
    yield
    if state.client is not None:
        await state.client.aclose()
        state.client = None


@pytest.mark.usefixtures("_mcp_state")
async def test_mcp_describe_and_invoke_skill(app, client: httpx.AsyncClient, sdk: Make) -> None:
    admin_key, caller_key, executor_key = await _setup(client)
    async with sdk(admin_key) as admin:
        await admin.register_skill(
            name="math.double", side_effects="none", risk_level="low", contract=CONTRACT
        )
        await admin.register_skill(name="bidops.fetch", protocol="http")
    mcp_server.STATE.client = ControlPlaneClient(
        "http://testserver", caller_key, transport=httpx.ASGITransport(app=app)
    )

    described = json.loads(await mcp_server.cp_describe_skill("math.double"))
    assert described["contract"]["implementation"]["protocol"] == "local"

    catalog_only = json.loads(await mcp_server.cp_invoke_skill("bidops.fetch", {}))
    assert catalog_only["error"] == "skill_not_invocable"

    # Nobody executes it: the bounded wait returns the pending invocation.
    pending = json.loads(
        await mcp_server.cp_invoke_skill("math.double", {"n": 1}, wait_seconds=0.2)
    )
    assert pending["status"] == "pending"

    async def execute_soon() -> None:
        async with sdk(executor_key) as executor:
            for _ in range(50):
                claimed = await executor.claim_skill_invocation(
                    protocols=["local"], local_entrypoints=["cp_skills.math:double"]
                )
                if claimed and claimed["invocation"]["inputs"] == {"n": 3}:
                    invocation = claimed["invocation"]
                    await executor.complete_skill_invocation(
                        invocation["id"],
                        fencing_token=invocation["fencingToken"],
                        output={"double": invocation["inputs"]["n"] * 2},
                    )
                    return
                await asyncio.sleep(0.05)

    worker = asyncio.create_task(execute_soon())
    result = json.loads(await mcp_server.cp_invoke_skill("math.double", {"n": 3}, wait_seconds=10))
    await worker
    assert result["status"] == "succeeded"
    assert result["output"] == {"double": 6}

    # Invoking is an authoritative write: a nested executor does not get it.
    withheld = await mcp_server.withheld_tool_names()
    assert "cp_invoke_skill" in withheld
    assert "cp_describe_skill" not in withheld


async def test_sdk_passes_the_executor_session(client: httpx.AsyncClient, sdk: Make) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=["skills.execute", "sessions.open"]
    )
    work_session = await open_session(client, executor_key)
    async with sdk(admin_key) as admin, sdk(executor_key) as executor:
        await admin.register_skill(
            name="math.double", side_effects="none", risk_level="low", contract=CONTRACT
        )
        await admin.invoke_skill("math.double", inputs={"n": 2})
        claimed = await executor.claim_skill_invocation(
            protocols=["local"],
            local_entrypoints=["cp_skills.math:double"],
            session_id=work_session["id"],
        )
        assert claimed is not None
        lease = claimed["invocation"]
        token = lease["fencingToken"]

        with pytest.raises(ControlPlaneError) as stale:
            await executor.heartbeat_skill_invocation(lease["id"], fencing_token=token)
        assert stale.value.code == "stale_invocation_lease"
        await executor.heartbeat_skill_invocation(
            lease["id"], fencing_token=token, session_id=work_session["id"]
        )
        retried = await executor.fail_skill_invocation(
            lease["id"],
            fencing_token=token,
            code="flaky",
            retryable=True,
            session_id=work_session["id"],
        )
        assert retried["status"] == "pending"

        claimed = await executor.claim_skill_invocation(
            protocols=["local"],
            local_entrypoints=["cp_skills.math:double"],
            session_id=work_session["id"],
        )
        assert claimed is not None
        done = await executor.complete_skill_invocation(
            lease["id"],
            fencing_token=claimed["invocation"]["fencingToken"],
            output={"double": 4},
            session_id=work_session["id"],
        )
        assert done["status"] == "succeeded"
