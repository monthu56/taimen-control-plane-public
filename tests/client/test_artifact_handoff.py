"""Artifact handoff through the client, MCP and the runner (CP-ADR-0072, A007).

The acceptance of A007: a runner receives the inputs of its task as files and
sees them in the prompt; the agent hands a file in as an output over MCP. The
SDK calls underneath — upload, artifact by ``contentRef``, download as an
input of another task — are exercised against the real application.
"""

import hashlib
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import control_plane_mcp.server as mcp_server
from control_plane.infrastructure.content_store import InMemoryContentStore
from control_plane_agent.inputs import LocalInput
from control_plane_agent.instructions import build_prompt
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.workspace import Workspace
from control_plane_client import ControlPlaneClient, ControlPlaneError
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
)

Make = Callable[[str], ControlPlaneClient]
RUNNER_PERMISSIONS = [*ORG_AGENT_PERMISSIONS, "task_types.read"]
SPEC = b"# Spec\n\nThe feature does one thing.\n"
PLAN = b"# Plan\n\n1. Do the thing.\n"


@pytest.fixture
def store(app: FastAPI) -> InMemoryContentStore:
    content_store = InMemoryContentStore()
    app.state.content_store = content_store
    return content_store


@pytest.fixture(autouse=True)
async def _fresh_mcp_state() -> AsyncIterator[None]:
    state = mcp_server.STATE
    state.client = None
    state.session_id = None
    state.claim_id = None
    state.fencing_token = None
    state.run_id = None
    state.task_ref = None
    state.heartbeats = None
    state.session_lock = None
    yield
    state.client = None
    state.run_id = None
    state.task_ref = None


def _read(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


async def boot(client: httpx.AsyncClient) -> dict[str, Any]:
    body = await do_bootstrap(client)
    admin = body["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin, name="runner", permissions=RUNNER_PERMISSIONS
    )
    for key in ("spec-document", "plan-document"):
        response = await client.post(
            "/api/v1/artifact-types",
            json={"key": key, "displayName": key, "mediaTypes": ["text/*"]},
            headers=auth(admin),
        )
        assert response.status_code == 201, response.text
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "planning",
            "displayName": "Planning",
            "artifactSchema": {
                "inputs": [
                    {"key": "spec", "type": "spec-document", "from": "spawned_by", "required": True}
                ]
            },
        },
        headers=auth(admin),
    )
    assert response.status_code == 201, response.text
    return {"admin": admin, "agent": agent_key, "agentId": agent["id"]}


async def test_sdk_uploads_references_and_downloads_content(
    client: httpx.AsyncClient, sdk: Make, store: InMemoryContentStore, tmp_path: Path
) -> None:
    keys = await boot(client)
    source = await create_task(client, keys["admin"], title="Design")
    consumer = await create_task(client, keys["admin"], title="Plan", typeKey="planning")

    async with sdk(keys["admin"]) as admin:
        from_bytes = await admin.upload_artifact_content(SPEC, media_type="text/markdown")
        assert from_bytes["contentRef"].startswith("cref_")
        assert from_bytes["sizeBytes"] == len(SPEC)
        assert from_bytes["sha256"] == hashlib.sha256(SPEC).hexdigest()

        # A path is streamed from disk; the same bytes are the same object.
        spec_file = tmp_path / "spec.md"
        spec_file.write_bytes(SPEC)
        from_path = await admin.upload_artifact_content(spec_file, media_type="text/markdown")
        assert from_path["sha256"] == from_bytes["sha256"]
        assert from_path["contentRef"] != from_bytes["contentRef"]

        spec = await admin.create_artifact(
            type="spec-document",
            name="spec.md",
            task_ref=source["id"],
            content_ref=from_path["contentRef"],
        )
        assert spec["contentState"] == "stored"
        assert spec["mediaType"] == "text/markdown"
        await admin.add_task_relation(
            consumer["id"], to_task=source["id"], relation_type="spawned_by"
        )

    async with sdk(keys["agent"]) as agent:
        got = await agent.download_artifact_content(
            spec["id"], tmp_path / "in" / "spec.md", for_task=consumer["publicId"]
        )
        assert _read(got["path"]) == SPEC
        assert got["sha256"] == spec["sha256"]
        assert got["sizeBytes"] == len(SPEC)
        assert got["mediaType"].startswith("text/markdown")
        # Only the file itself is left behind: no partial file beside it.
        assert _names(tmp_path / "in") == ["spec.md"]

        with pytest.raises(ControlPlaneError) as missing:
            await agent.download_artifact_content(
                "00000000-0000-0000-0000-000000000000", tmp_path / "in" / "x"
            )
        assert missing.value.status == 404
        assert not (tmp_path / "in" / "x").exists()


async def test_a_download_that_does_not_match_its_sha256_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"tampered",
            headers={"Content-Type": "text/plain", "ETag": f'"sha256:{"0" * 64}"'},
        )

    async with ControlPlaneClient(
        "http://testserver", "cp_x", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ControlPlaneError) as exc:
            await client.download_artifact_content("a", tmp_path / "file.txt")
    assert exc.value.code == "content_integrity_failed"
    assert _names(tmp_path) == []


class PlanningAdapter:
    """Reads its input from disk, writes a plan, hands it in over MCP."""

    def __init__(self, app: FastAPI, agent_key: str, out_dir: Path) -> None:
        self.app = app
        self.agent_key = agent_key
        self.out_dir = out_dir
        self.prompt = ""
        self.inputs: list[LocalInput] = []
        self.mcp_answer: dict[str, Any] = {}

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
        inputs: Sequence[LocalInput] | None = None,
    ) -> list[ArtifactSpec]:
        import json

        self.inputs = list(inputs or [])
        context = await client.get_working_context(task_ref=task["id"], run_id=run["id"])
        self.prompt = build_prompt(task, context, inputs=inputs)
        spec = next(entry for entry in self.inputs if entry.key == "spec")
        assert spec.path is not None
        plan = self.out_dir / "plan.md"
        plan.write_bytes(PLAN + spec.path.read_bytes())

        # The agent's MCP server, as the Claude Code adapter starts it.
        mcp_server.adopt_run_from_environment(
            {"CONTROL_PLANE_TASK": str(task["id"]), "CONTROL_PLANE_RUN_ID": str(run["id"])}
        )
        mcp_server.STATE.client = ControlPlaneClient(
            "http://testserver", self.agent_key, transport=httpx.ASGITransport(app=self.app)
        )
        try:
            self.mcp_answer = json.loads(
                await mcp_server.cp_create_artifact(type="plan-document", file=str(plan))
            )
            read_back = json.loads(
                await mcp_server.cp_get_artifact_content(
                    spec.artifact_id, path=str(self.out_dir / "again.md")
                )
            )
            assert read_back["text"] == SPEC.decode()
        finally:
            await mcp_server.STATE.client.aclose()
        return []


async def test_runner_gets_inputs_as_files_and_hands_in_an_output_over_mcp(
    client: httpx.AsyncClient,
    app: FastAPI,
    sdk: Make,
    store: InMemoryContentStore,
    tmp_path: Path,
) -> None:
    keys = await boot(client)
    engineering = await create_workspace(client, keys["admin"], "engineering")
    source = await create_task(client, keys["admin"], title="Design")
    consumer = await create_task(
        client,
        keys["admin"],
        title="Plan the feature",
        typeKey="planning",
        workspaceId=engineering["id"],
    )
    relation = await client.post(
        f"/api/v1/tasks/{consumer['id']}/relations",
        json={"toTask": source["id"], "type": "spawned_by"},
        headers=auth(keys["admin"]),
    )
    assert relation.status_code == 201, relation.text
    upload = await client.put(
        "/api/v1/artifact-contents",
        content=SPEC,
        headers={**auth(keys["admin"]), "Content-Type": "text/markdown"},
    )
    assert upload.status_code == 201, upload.text
    spec = await client.post(
        "/api/v1/artifacts",
        json={
            "task": source["id"],
            "type": "spec-document",
            "name": "../spec.md",
            "contentRef": upload.json()["contentRef"],
        },
        headers=auth(keys["admin"]),
    )
    assert spec.status_code == 201, spec.text

    runtime = tmp_path / "runtime"
    out_dir = tmp_path / "agent"
    out_dir.mkdir()
    adapter = PlanningAdapter(app, keys["agent"], out_dir)
    async with sdk(keys["agent"]) as agent_client:
        agent = Agent(
            agent_client,
            adapter,
            poll_interval=0.05,
            max_cycles=2,
            workspace_id=engineering["id"],
            runtime_dir=runtime,
        )
        await agent.run_forever()

    # The input reached the adapter as a file under the task's runtime dir,
    # its name reduced to one path component.
    [entry] = adapter.inputs
    expected = runtime / consumer["publicId"] / "inputs" / "spec" / "spec.md"
    assert entry.path == expected
    assert entry.source_task == source["publicId"]
    assert entry.relation == "spawned_by"
    # ...and the prompt names it, inside the data fence.
    section = adapter.prompt.split("## Входы", 1)[1]
    fenced = section.split("<task_inputs>", 1)[1].split("</task_inputs>", 1)[0]
    assert str(expected) in fenced
    assert source["publicId"] in fenced
    # A finished run leaves no downloaded inputs behind.
    assert not expected.exists()

    record = await client.get(f"/api/v1/tasks/{consumer['id']}", headers=auth(keys["admin"]))
    assert record.json()["status"] == "done"
    runs = (
        await client.get(
            "/api/v1/runs", params={"taskId": consumer["id"]}, headers=auth(keys["admin"])
        )
    ).json()["items"]
    output = adapter.mcp_answer
    assert output["type"] == "plan-document", output
    assert output["name"] == "plan.md"
    assert output["taskId"] == consumer["id"]
    assert output["runId"] == runs[0]["id"]
    assert output["contentState"] == "stored"
    assert output["mediaType"] == "text/markdown"
    content = await client.get(
        f"/api/v1/artifacts/{output['id']}/content", headers=auth(keys["admin"])
    )
    assert content.content == PLAN + SPEC


async def test_mcp_file_artifact_refuses_a_mix_and_a_missing_file(
    client: httpx.AsyncClient, app: FastAPI, store: InMemoryContentStore, tmp_path: Path
) -> None:
    import json

    keys = await boot(client)
    mcp_server.STATE.client = ControlPlaneClient(
        "http://testserver", keys["agent"], transport=httpx.ASGITransport(app=app)
    )
    try:
        mixed = json.loads(
            await mcp_server.cp_create_artifact(
                type="report", file=str(tmp_path / "a.txt"), uri="https://example.org"
            )
        )
        assert mixed["error"] == "invalid_artifact_content"
        missing = json.loads(
            await mcp_server.cp_create_artifact(type="report", file=str(tmp_path / "nope.txt"))
        )
        assert missing["error"] == "file_not_found"
        nameless = json.loads(await mcp_server.cp_create_artifact(type="report"))
        assert nameless["error"] == "invalid_request"
    finally:
        await mcp_server.STATE.client.aclose()
