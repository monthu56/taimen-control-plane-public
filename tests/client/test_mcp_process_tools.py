"""MCP tools that read processes against a real test Control Plane (CP-ADR-0074 §17; P016).

Each tool is a thin adapter over a route of the core; what is checked here
is what the adapter adds — the one shape of every error (the finding of
P006) and the explanation of an instance assembled from its journal, its
version and memory's answers. The package tools (check, test, plan, apply)
moved to the author server of package-sdk (``package-sdk mcp``, S025).
"""

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import control_plane_mcp.server as mcp_server
from control_plane.worker.main import Worker
from control_plane_client import ControlPlaneClient
from tests.integration.test_package_test import PROCESS, package
from tests.integration.test_package_test import _setup as catalog_setup
from tests.integration.test_process_instances import _instances, _observe, _publish
from tests.integration.test_process_memory import CASE, _process, memory, worker
from tests.integration.test_process_memory import _setup as memory_setup
from tests.integration.test_process_sla import _flow, _start
from tests.integration.test_process_sla import _setup as sla_setup

__all__ = ["memory", "worker"]

FINDING = {"code", "severity", "path", "file", "line", "message", "hint"}


@pytest.fixture(autouse=True)
async def _fresh_state() -> Any:
    state = mcp_server.STATE
    state.client = None
    state.session_id = None
    state.run_id = None
    state.task_ref = None
    yield
    if state.client is not None:
        await state.client.aclose()
        state.client = None


def _wire(app: FastAPI, api_key: str) -> None:
    mcp_server.STATE.client = ControlPlaneClient(
        "http://testserver", api_key, transport=httpx.ASGITransport(app=app)
    )


def _load(payload: str) -> Any:
    return json.loads(payload)


def _refused(payload: str, code: str) -> dict[str, Any]:
    """An error of the tools: its code and at least one finding in the shape of P006."""
    body: dict[str, Any] = _load(payload)
    assert body["error"] == code, body
    assert body["problems"], body
    assert all(set(p) == FINDING for p in body["problems"]), body["problems"]
    return body


async def test_process_get_reads_a_version_and_the_versions(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    key = await catalog_setup(client)
    _wire(app, key)
    _refused(await mcp_server.cp_process_get("sample"), "not_found")
    for version in (1, 2):
        # Published the way the SDK's author server does: a plan, then its hash.
        files = package(process=PROCESS.replace("version: 1", f"version: {version}"))["files"]
        plan = await mcp_server._client().plan_package(files)
        await mcp_server._client().apply_package(files, plan_hash=plan["planHash"])

    latest = _load(await mcp_server.cp_process_get("sample"))
    assert (latest["definition"]["version"], latest["definition"]["latestVersion"]) == (2, 2)
    assert latest["definition"]["identityAgent"] == "sample-process"
    assert [v["version"] for v in latest["versions"]["items"]] == [2, 1]
    first = _load(await mcp_server.cp_process_get("sample@1"))
    assert first["definition"]["version"] == 1
    assert first["definition"]["definitionHash"] != latest["definition"]["definitionHash"]


async def test_explain_gives_decisions_with_reasons_memory_and_regulations(
    client: httpx.AsyncClient, app: FastAPI, worker: Worker
) -> None:
    s = await memory_setup(client)
    key = s["key"]
    # Version 2 names its regulations: the process, and a section at the review.
    spec = _process(s["admin"], s["workspace"])
    spec["version"] = 2
    spec["governedBy"] = [{"document": "regulation:cases"}]
    review = spec["stages"][0]["steps"][2]
    assert review["id"] == "review"
    review["governedBy"] = [{"document": "regulation:cases", "section": "3"}]
    await _publish(client, key, "sample-memory", spec)
    await _observe(client, key, "sample.opened", number="S-1", amount=1200)
    await worker.run_once()
    [instance] = await _instances(client, key, definitionKey="sample-memory")
    _wire(app, key)

    explained = _load(await mcp_server.cp_process_explain(instance["id"]))

    assert explained["instance"]["id"] == instance["id"]
    process = explained["process"]
    assert (process["key"], process["version"]) == ("sample-memory", 2)
    assert process["governedBy"] == [{"document": "regulation:cases", "element": None}]
    steps = explained["steps"]
    assert steps[0]["input"].startswith("start")
    assert [s["seq"] for s in steps] == sorted(s["seq"] for s in steps)
    decisions = [d for step in steps for d in step["decisions"]]
    assert all(d["reason"] for d in decisions)

    # The review answers to its section first, then to the process.
    [opened] = [i for step in steps for i in step["intents"] if i["element"] == "review"]
    assert opened["data"]["intent"] == "create_task"
    assert opened["governedBy"] == [
        {"document": "regulation:cases", "section": "3", "element": "review"},
        {"document": "regulation:cases", "element": None},
    ]
    history = [d for d in decisions if d["element"] == "history"]
    assert history and all(
        d["governedBy"] == [{"document": "regulation:cases", "element": None}] for d in history
    )

    # What memory answered to the recall step, as the journal recorded it.
    [answer] = explained["memory"]
    assert (answer["element"], answer["status"]) == ("history", "completed")
    assert [n["key"] for n in answer["result"]["nodes"]] == [CASE, "lesson:1", "lesson:similar"]
    assert explained["journalCursor"] is None

    missing = _refused(
        await mcp_server.cp_process_explain("00000000-0000-0000-0000-000000000000"), "not_found"
    )
    assert missing["problems"][0]["severity"] == "error"


async def test_get_and_explain_show_the_deadlines_and_their_sla_state(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """CP-ADR-0078 §6 (P014): declared deadlines of a version, their state in an instance."""
    s = await sla_setup(client)
    key = s["key"]
    spec = _flow(s["admin"], {"duration": "PT1H", "warnBefore": "PT15M"})
    spec["due"] = "P2D"
    await _publish(client, key, "sample-deadline", spec)
    instance = await _start(client, key, "sample-deadline")
    _wire(app, key)

    got = _load(await mcp_server.cp_process_get("sample-deadline"))
    assert got["deadlines"] == {
        "process": "P2D",
        "steps": [
            {
                "element": "review",
                "kind": "human",
                "due": {"duration": "PT1H", "warnBefore": "PT15M"},
            }
        ],
    }

    explained = _load(await mcp_server.cp_process_explain(instance["id"]))
    sla = explained["sla"]
    assert sla["slaState"] == "ok"
    assert sla["process"]["dueAt"] == instance["sla"]["dueAt"]
    [step] = sla["steps"]
    assert (step["element"], step["attempt"], step["slaState"]) == ("review", 1, "ok")
    assert step["due"]["warnAt"] is not None
    assert step["overdueSeconds"] is None


async def test_the_author_tools_of_packages_are_not_here() -> None:
    """Package tools live in the SDK's author server (package-sdk mcp, FR-002).

    What stays here reads the catalog and writes nothing, so a nested
    executor gets it.
    """
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert not {name for name in names if name.startswith("cp_pkg_")}
    assert {"cp_process_get", "cp_process_explain"} <= names
    withheld = await mcp_server.withheld_tool_names()
    assert "cp_process_get" not in withheld
    assert "cp_process_explain" not in withheld
