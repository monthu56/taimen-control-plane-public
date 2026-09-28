"""MCP tools of a process author against a real test Control Plane (CP-ADR-0074 §17; P016).

The package is a directory on disk, as the author has it: the fixture
package of ``packages:test`` (``tests/fixtures/processes/sample.process.yaml``
with its test scenario) written out file by file. Every tool is a thin
adapter over a route of the core; what is checked here is what the adapter
adds — reading the directory, the one shape of every error (the finding of
P006), the refusal to apply without the hash of a plan, and the explanation
of an instance assembled from its journal, its version and memory's answers.
"""

import json
from pathlib import Path
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


def _write(root: Path, files: list[dict[str, str]]) -> Path:
    for item in files:
        target = root / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
    return root


def _fixture_package(root: Path, **overrides: Any) -> Path:
    return _write(root, package(**overrides)["files"])


def _load(payload: str) -> Any:
    return json.loads(payload)


def _refused(payload: str, code: str) -> dict[str, Any]:
    """An error of the tools: its code and at least one finding in the shape of P006."""
    body: dict[str, Any] = _load(payload)
    assert body["error"] == code, body
    assert body["problems"], body
    assert all(set(p) == FINDING for p in body["problems"]), body["problems"]
    return body


async def test_check_and_test_read_the_package_directory(
    client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    _wire(app, await catalog_setup(client))
    root = _fixture_package(tmp_path / "sample")
    # Not part of the package: hidden directories, files that are not YAML.
    _write(root, [{"path": ".layout/sample.yaml", "content": "{not: [yaml"}])
    _write(root, [{"path": "README.md", "content": "# sample"}])

    checked = _load(await mcp_server.cp_pkg_check(str(root)))
    assert checked["status"] == "passed"
    (warning,) = checked["problems"]
    assert set(warning) == FINDING
    assert (warning["code"], warning["file"], warning["line"]) == (
        "governed_by_unchecked",
        "processes/sample.yaml",
        7,
    )

    tested = _load(await mcp_server.cp_pkg_test(str(root)))
    assert tested["status"] == "passed", tested["tests"]
    assert tested["checkOnly"] is False
    (result,) = tested["tests"]
    assert (result["file"], result["status"]) == ("tests/review.test.yaml", "passed")
    assert tested["coverage"][0]["process"] == "sample"

    only = _load(await mcp_server.cp_pkg_test(str(root), tests=["tests/absent.test.yaml"]))
    assert only["tests"] == []


async def test_an_invalid_package_names_the_file_and_line(
    client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    _wire(app, await catalog_setup(client))
    broken = PROCESS.replace("taskType: review", "taskType: reveiw")
    root = _fixture_package(tmp_path / "broken", process=broken)

    checked = _load(await mcp_server.cp_pkg_check(str(root)))
    assert checked["status"] == "invalid"
    (error,) = [p for p in checked["problems"] if p["severity"] == "error"]
    assert (error["code"], error["file"]) == ("unknown_task_type", "processes/sample.yaml")
    assert broken.splitlines()[error["line"] - 1].strip() == "taskType: reveiw"


async def test_a_directory_that_is_no_package_is_a_finding(
    client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    _wire(app, await catalog_setup(client))
    missing = _refused(await mcp_server.cp_pkg_check(str(tmp_path / "absent")), "package_not_found")
    assert missing["problems"][0]["severity"] == "error"
    (tmp_path / "empty").mkdir()
    _refused(await mcp_server.cp_pkg_test(str(tmp_path / "empty")), "package_empty")
    unreadable = _write(tmp_path / "binary", [{"path": "package.yaml", "content": "k: v"}])
    (unreadable / "processes").mkdir()
    (unreadable / "processes" / "bad.yaml").write_bytes(b"\xff\xfe")
    body = _refused(await mcp_server.cp_pkg_plan(str(unreadable)), "package_file_unreadable")
    assert body["problems"][0]["file"] == "processes/bad.yaml"


async def test_apply_needs_the_hash_of_the_plan_and_refuses_a_stale_one(
    client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    _wire(app, await catalog_setup(client))
    root = _fixture_package(tmp_path / "sample")

    # Without a hash, or with something that is not one, nothing reaches the core.
    for no_hash in ("", "latest", "sha256:abc"):
        body = _refused(await mcp_server.cp_pkg_apply(str(root), no_hash), "plan_hash_required")
        assert "cp_pkg_plan" in body["problems"][0]["hint"]
    _refused(await mcp_server.cp_process_get("sample"), "not_found")

    plan = _load(await mcp_server.cp_pkg_plan(str(root)))
    assert plan["planHash"].startswith("sha256:")
    assert [(c["kind"], c["key"], c["action"]) for c in plan["changes"]] == [
        ("Process", "sample", "create")
    ]
    assert [p["code"] for p in plan["problems"] if p["severity"] == "error"] == []
    _refused(await mcp_server.cp_process_get("sample"), "not_found")  # a plan writes nothing

    applied = _load(await mcp_server.cp_pkg_apply(str(root), plan["planHash"]))
    assert applied["planHash"] == plan["planHash"]
    assert applied["applied"] == [
        {"kind": "Process", "key": "sample", "action": "create", "version": 1}
    ]

    # The same hash again: the catalog is not what the plan was built against.
    stale = _refused(await mcp_server.cp_pkg_apply(str(root), plan["planHash"]), "plan_stale")
    assert stale["details"]["planHash"] == plan["planHash"]
    assert stale["details"]["currentPlanHash"] != plan["planHash"]
    assert "cp_pkg_plan" in stale["hint"]
    assert stale["problems"][0]["code"] == "plan_stale"

    # The files changed after the plan: the plan the user saw is not this package.
    fresh = _load(await mcp_server.cp_pkg_plan(str(root)))
    assert [c["action"] for c in fresh["changes"]] == ["unchanged"]
    _fixture_package(root, process=PROCESS.replace("version: 1", "version: 2"))
    _refused(await mcp_server.cp_pkg_apply(str(root), fresh["planHash"]), "plan_stale")
    got = _load(await mcp_server.cp_process_get("sample"))
    assert got["definition"]["version"] == 1


async def test_a_refused_apply_returns_the_findings_of_the_core(
    client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    _wire(app, await catalog_setup(client))
    root = _fixture_package(tmp_path / "broken", process=PROCESS.replace("review", "reveiw", 1))
    plan = _load(await mcp_server.cp_pkg_plan(str(root)))
    assert [p["code"] for p in plan["problems"] if p["severity"] == "error"]
    body = _refused(await mcp_server.cp_pkg_apply(str(root), plan["planHash"]), "invalid_package")
    assert any(p["file"] == "processes/sample.yaml" and p["line"] for p in body["problems"])


async def test_process_get_reads_a_version_and_the_versions(
    client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    key = await catalog_setup(client)
    _wire(app, key)
    root = _fixture_package(tmp_path / "sample")
    for version in (1, 2):
        _fixture_package(root, process=PROCESS.replace("version: 1", f"version: {version}"))
        plan = _load(await mcp_server.cp_pkg_plan(str(root)))
        _load(await mcp_server.cp_pkg_apply(str(root), plan["planHash"]))

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


async def test_only_apply_is_withheld_from_a_nested_executor() -> None:
    """Check, test, plan and reading write nothing; applying changes the catalog."""
    withheld = await mcp_server.withheld_tool_names()
    assert "cp_pkg_apply" in withheld
    for tool in (
        "cp_pkg_check",
        "cp_pkg_test",
        "cp_pkg_plan",
        "cp_process_get",
        "cp_process_explain",
    ):
        assert tool not in withheld
