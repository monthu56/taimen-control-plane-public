"""The daemon reads the task's comments into the prompt (TASK-001131, TASK-001132).

Against the real API: what a person wrote on the task reaches the executor,
edited comments as edited, and the executor's own "blocked" comment from an
earlier run does not come back to it as if someone else had said it. The
author is named from the thread itself, so a runner without
``principals.read`` still tells the owner from an agent.
"""

from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane_agent.blocked import CHECKPOINT_KIND
from control_plane_agent.comments import HEADING
from control_plane_agent.instructions import build_prompt
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.workspace import ExecutionWorkspacePool, Workspace
from control_plane_client import ControlPlaneClient
from tests.client.test_agent import RUNNER_PERMISSIONS, Make
from tests.client.test_agent_blocked import _origin
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap


class PromptAdapter:
    """Keeps the prompt of every run; blocked on the first one when told to."""

    def __init__(self, *, block_first: bool) -> None:
        self.block_first = block_first
        self.prompts: list[str] = []

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        self.prompts.append(build_prompt(task, {}))
        if self.block_first and len(self.prompts) == 1:
            await client.create_checkpoint(
                str(run["id"]), kind=CHECKPOINT_KIND, data={"reason": "Needs a person."}
            )
        return [ArtifactSpec(type="report", name="summary", content={"summary": "ok"})]


async def _comment(client: httpx.AsyncClient, key: str, task_id: str, body: str) -> Any:
    response = await client.post(
        f"/api/v1/tasks/{task_id}/comments", json={"body": body}, headers=auth(key)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _work(sdk: Make, key: str, adapter: PromptAdapter, pool: ExecutionWorkspacePool) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(sdk_client, adapter, poll_interval=0.05, max_cycles=2, workspaces=pool)
        await agent.run_forever()


@pytest.mark.parametrize(
    "permissions", [RUNNER_PERMISSIONS, [*RUNNER_PERMISSIONS, "principals.read"]]
)
async def test_comments_reach_the_prompt_without_the_executors_own_block(
    client: httpx.AsyncClient,
    sdk: Make,
    tmp_path: Path,
    permissions: list[str],
) -> None:
    assert "principals.read" not in RUNNER_PERMISSIONS
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    principal, agent_key = await create_agent_with_key(
        client, admin, name="bot", permissions=permissions
    )
    task = await create_task(client, admin, title="Change a file")
    task_id = task["id"]
    await _comment(client, admin, task_id, "Сначала U002, потом U005.")
    edited = await _comment(client, admin, task_id, "Черновик уточнения.")
    response = await client.patch(
        f"/api/v1/tasks/{task_id}/comments/{edited['id']}",
        json={"body": "Уточнение: U005 не трогать."},
        headers={**auth(admin), "If-Match": '"comment-1"'},
    )
    assert response.status_code == 200, response.text
    pool = ExecutionWorkspacePool(_origin(tmp_path), tmp_path / "workspaces")
    adapter = PromptAdapter(block_first=True)

    await _work(sdk, agent_key, adapter, pool)
    assert len(adapter.prompts) == 1
    first = adapter.prompts[0]
    assert first.index("Сначала U002") < first.index("Уточнение: U005 не трогать.")
    assert "Черновик уточнения." not in first
    assert "### Admin (человек)" in first

    # The run was blocked: the daemon's comment is on the task now.
    thread = await client.get(f"/api/v1/tasks/{task_id}/comments", headers=auth(admin))
    assert any("executor_blocked" in c["body"] for c in thread.json()["items"])
    record = (await client.get(f"/api/v1/tasks/{task_id}", headers=auth(admin))).json()
    response = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "todo"},
        headers={**auth(admin), "If-Match": f'"task-{record["version"]}"'},
    )
    assert response.status_code == 200, response.text
    await _comment(client, admin, task_id, "Вернул в работу: схема — первая.")

    await _work(sdk, agent_key, adapter, pool)
    assert len(adapter.prompts) == 2
    second = adapter.prompts[1]
    section = second[second.index(HEADING) :]
    assert "Вернул в работу: схема — первая." in section
    assert "Уточнение: U005 не трогать." in section
    assert "executor_blocked" not in section
    assert "Needs a person." not in section
    # Authors are words, not ids.
    assert principal["id"] not in section
