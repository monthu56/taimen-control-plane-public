"""Processes and memory on Postgres (CP-ADR-0076 §4-§6; process-packages P010).

The acceptance of P010: with a memory stub, a ``recall`` step is asked of
memory by the worker after the step's transaction and its answer comes back
as the instance's input, recorded in the journal; a ``remember`` step is an
observation of the core from the process's identity; the task of a human
step carries the step's context and its pack reads the case's explicit
links first, then what similarity found, marked. Replaying the instance
with a memory that fails on any call gives zero discrepancies (SC-011).

Memory is ``tests.fake_graph_memory.FakeGraphMemory`` — every typed request
is checked against Memory's pinned contract — with a graph of the case and
its lessons, and a read by similarity that finds one more lesson.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jsonschema
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from control_plane.application.commands import process_instances
from control_plane.application.context import graph
from control_plane.application.queries.recall import fetch_process_recall
from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.infrastructure.context_provider.base import ContextProviderError
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import ProcessInstance, ProcessRecall, SkillInvocation
from control_plane.worker.main import Worker
from tests.fake_graph_memory import Edge, FakeGraphMemory, Node
from tests.helpers import auth, claim_task, create_workspace, do_bootstrap, open_session
from tests.integration.test_process_instances import (
    AGENT,
    _events,
    _instance,
    _instances,
    _journal,
    _link_agent,
    _observe,
    _open,
    _publish,
    _task,
)
from tests.unit.test_process_contract import retrospective_contract

AGENT_PERMISSIONS = [
    "approvals.manage",
    "events.read",
    "observations.write",
    "skills.invoke",
    "tasks.read",
    "tasks.write",
]
CASE = "sample:S-1"


class CaseMemory(FakeGraphMemory):
    """The case, a lesson that applies to it, and one found only by similarity."""

    def __init__(self, *, fail: str | None = None) -> None:
        super().__init__(fail=fail)
        self.nodes = {
            n.key: n
            for n in (
                Node(CASE, "case", "Case S-1"),
                Node("lesson:1", "lesson", "Ask for the deadline in writing"),
                Node("lesson:similar", "lesson", "A similar case was late"),
            )
        }
        self.edges = [Edge("lesson:1", "applies_to", CASE, fact_id="f-lesson-1")]
        self.rejected = False

    async def typed_context(self, **kwargs: Any) -> dict[str, Any]:
        request = kwargs["request"]
        if self.rejected:
            raise ContextProviderError("bad request", retryable=False, status=400)
        if not request.get("allow_semantic"):
            return await super().typed_context(**kwargs)
        # Similarity: whatever the text, the similar lesson, marked as Memory marks it.
        found = {**request, "anchors": [{"kind": "lesson", "value": "lesson:similar"}]}
        pack = await super().typed_context(**{**kwargs, "request": found})
        self.typed_requests[-1] = {**request, "scope": self.typed_requests[-1]["scope"]}
        for section in pack["sections"]:
            for item in section["items"]:
                item["evidence"] = "inferred"
        return pack


class NoMemory:
    """A memory client that fails on any call: replay must not make one."""

    calls = 0

    def __getattr__(self, name: str) -> Any:
        async def fail(*args: Any, **kwargs: Any) -> Any:
            NoMemory.calls += 1
            raise AssertionError(f"memory was called: {name}")

        return fail


@pytest.fixture
def memory(app: FastAPI) -> AsyncIterator[CaseMemory]:
    graph._pack_patterns.clear()
    fake = CaseMemory()
    app.state.context_provider = fake
    yield fake
    app.state.context_provider = None


@pytest.fixture
async def worker(settings: Settings, memory: CaseMemory) -> AsyncIterator[Worker]:
    instance = Worker(settings, graph_provider=memory)  # type: ignore[arg-type]
    yield instance
    await instance.engine.dispose()


def _process(admin: str, workspace: str, *, timeout: str = "PT10M") -> dict[str, Any]:
    person = [{"principal": admin}]
    return {
        "version": 1,
        "displayName": "Case with memory",
        "workspaceId": workspace,
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "number": {"type": "string"},
                "amount": {"type": "number"},
                "history": {"type": "array"},
                "note": {"type": "string"},
            },
        },
        "memory": {"case": {"key": "'sample:' + data.number"}},
        "start": {
            "on": {"observation": "sample.opened"},
            "key": "event.payload.data.number",
            "set": {
                "number": "string(event.payload.data.number)",
                "amount": "double(event.payload.data.amount)",
            },
        },
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "history",
                        "recall": {
                            "anchors": [{"case": True}],
                            "traverse": [{"relation": "applies_to", "direction": "in"}],
                            "query": "'cases like ' + data.number",
                            "timeout": timeout,
                            "onTimeout": [{"id": "no-memory", "set": {"note": "'no memory'"}}],
                        },
                        "output": {"as": {"history": "step.result.nodes"}},
                    },
                    {"id": "price", "remember": {"facts": {"amount": "data.amount"}}},
                    {
                        "id": "review",
                        "human": {
                            "taskType": "review",
                            "assign": person,
                            "context": {
                                "anchors": [{"case": True}],
                                "traverse": [{"relation": "applies_to", "direction": "in"}],
                                "budgetTokens": 2000,
                            },
                        },
                    },
                    {"id": "done", "complete": {"outcome": "reviewed"}},
                ],
            }
        ],
    }


async def _setup(client: httpx.AsyncClient, **process: Any) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    responses = [
        await client.post(
            "/api/v1/agents",
            json={
                "key": AGENT,
                "spec": {
                    "displayName": "Sample process",
                    "identity": {"kind": "service", "permissions": AGENT_PERMISSIONS},
                    "placement": "none",
                },
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/task-types",
            json={
                "key": "review",
                "displayName": "Review",
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
            },
            headers=auth(key),
        ),
    ]
    for response in responses:
        assert response.status_code in (200, 201), response.text
    agent = await _link_agent(client, key)
    workspace = await create_workspace(client, key, "cases")
    admin = boot["adminPrincipal"]["id"]
    await _publish(client, key, "sample-memory", _process(admin, workspace["id"], **process))
    return {"key": key, "admin": admin, "agent": agent, "workspace": workspace["id"]}


async def _recalls(worker: Worker) -> list[ProcessRecall]:
    async with transaction(worker.session_factory) as session:
        return list((await session.scalars(select(ProcessRecall))).all())


# --- the acceptance of P010 --------------------------------------------------------


async def test_recall_remember_and_step_context_with_a_memory_stub(
    client: httpx.AsyncClient, worker: Worker, memory: CaseMemory, app: FastAPI
) -> None:
    s = await _setup(client)
    key = s["key"]

    await _observe(client, key, "sample.opened", number="S-1", amount=1200)
    await worker.run_once()
    [instance] = await _instances(client, key, definitionKey="sample-memory")

    # recall: asked by the worker after the step, from the case, at the input's time.
    [recall] = await _recalls(worker)
    assert (recall.state, recall.attempts, recall.element) == ("answered", 1, "history")
    explicit, semantic = memory.typed_requests
    assert explicit["anchors"] == [{"kind": "case", "value": CASE}]
    assert explicit["traverse"] == [
        {"relation": "applies_to", "direction": "in", "depth": 1, "limit": 20, "from": "anchors"}
    ]
    assert explicit["allow_semantic"] is False
    assert explicit["as_of"] == recall.request["asOf"]
    assert any(":ws:" in ns for ns in explicit["scope"]["namespaces"]), "the process's workspace"
    assert semantic["anchors"] == [{"value": "cases like S-1"}]
    assert semantic["allow_semantic"] is True

    # The answer is data of the instance: explicit links first, similarity marked.
    instance = await _instance(client, key, instance["id"])
    history = instance["data"]["history"]
    assert [(n["key"], n["inferred"]) for n in history] == [
        (CASE, False),
        ("lesson:1", False),
        ("lesson:similar", True),
    ]
    [completed] = await _events(client, key, "process.recall_completed")
    assert completed["payload"]["recallId"] == str(recall.id)
    assert (completed["payload"]["nodeCount"], completed["payload"]["edgeCount"]) == (3, 1)
    assert "nodes" not in completed["payload"], "the answer stays in the journal"
    journal = await _journal(client, key, instance["id"])
    [answered] = [
        e for e in journal if e["kind"] == "input" and e["data"]["input"]["kind"] == "recall"
    ]
    assert answered["data"]["input"]["body"]["result"]["nodes"] == history

    # remember: an observation of the core from the process's identity, in its workspace.
    [remembered] = [
        e
        for e in await _events(client, key, "observation.recorded")
        if e["payload"].get("source") == "process:sample-memory"
    ]
    assert remembered["actorId"] == s["agent"]
    payload = remembered["payload"]
    assert payload["kind"] == "process.remembered"
    assert payload["workspaceId"] == s["workspace"]
    assert payload["dedupKey"].startswith(f"process:{instance['id']}:price:")
    assert payload["assertions"] == [
        {
            "assert": "entity",
            "entity": {
                "key": f"case:{CASE}",
                "type": "case",
                "title": "",
                "properties": {"amount": 1200},
            },
        }
    ]
    assert payload["data"]["case"] == {"kind": "case", "key": CASE}

    # The step's context: its task carries the profile, the pack reads the case first.
    review = _open(instance, "review")
    task = await _task(client, key, review["taskId"])
    response = await client.post(
        "/api/v1/context", json={"task": task["id"], "query": "continue"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    context = response.json()["taskContext"]
    assert context["status"] == "ok", context
    assert context["anchors"] == [{"value": CASE, "kind": "case", "source": "step"}]
    sections = context["pack"]["sections"]
    assert [(s["kind"], s.get("inferred", False)) for s in sections] == [
        ("case", False),
        ("lesson", False),
        ("lesson", True),
    ]
    assert sections[-1]["items"][0]["natural_key"] == "lesson:similar"
    assert sections[-1]["items"][0]["evidence"] == "inferred"
    step_read, step_semantic = memory.typed_requests[2:]
    assert step_read["anchors"] == [{"kind": "case", "value": CASE}]
    assert step_semantic["allow_semantic"] is True
    assert step_semantic["anchors"] == [{"value": task["title"]}]


async def test_replay_of_an_instance_with_recall_never_asks_memory(
    client: httpx.AsyncClient, worker: Worker, memory: CaseMemory, app: FastAPI
) -> None:
    s = await _setup(client)
    await _observe(client, s["key"], "sample.opened", number="S-1", amount=1200)
    await worker.run_once()
    [instance] = await _instances(client, s["key"], definitionKey="sample-memory")
    assert "history" in instance["data"]

    # SC-011: a memory client failing on any call, the graph changed meanwhile.
    memory.edges.clear()
    worker.graph_provider = NoMemory()  # type: ignore[assignment]
    app.state.context_provider = NoMemory()
    NoMemory.calls = 0
    async with transaction(worker.session_factory) as session:
        row = await session.get(ProcessInstance, instance["id"])
        assert row is not None
        result = await process_instances.replay_instance(session, row)
    assert [d.out() for d in result.discrepancies] == []
    assert result.steps == 2
    assert NoMemory.calls == 0


async def test_memory_down_waits_for_the_step_timeout_and_a_late_answer_is_stale(
    client: httpx.AsyncClient,
    worker: Worker,
    memory: CaseMemory,
    settings: Settings,
    sync_engine: Engine,
) -> None:
    s = await _setup(client)
    key = s["key"]
    memory.fail = "typed"
    await _observe(client, key, "sample.opened", number="S-1", amount=1)
    await worker.run_once()
    [instance] = await _instances(client, key, definitionKey="sample-memory")
    [recall] = await _recalls(worker)
    assert recall.state == "pending" and recall.attempts == 1
    assert recall.last_error is not None and "memory_unavailable" in recall.last_error
    assert _open(instance, "history")["kind"] == "recall"

    # The answer of an attempt that began before the timeout arrives after it.
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE process_recalls SET next_attempt_at = now()"))
    memory.fail = None
    async with transaction(worker.session_factory) as session:
        call = await process_instances.begin_recall(session, recall.id, settings)
    assert call is not None
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE process_timers SET due_at = :at WHERE element = 'history'"),
            {"at": datetime.now(UTC) - timedelta(seconds=1)},
        )
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert instance["data"]["note"] == "no memory"
    [timed_out] = await _events(client, key, "process.recall_timed_out")
    assert timed_out["payload"]["reason"] == "timeout"

    answer = await fetch_process_recall(call, memory, settings)
    async with transaction(worker.session_factory) as session:
        await process_instances.finish_recall(session, recall.id, answer, settings)
    journal = await _journal(client, key, instance["id"])
    late = [e for e in journal if e["kind"] == "input" and e["data"]["input"]["kind"] == "recall"]
    assert len(late) == 1, "the late answer is recorded"
    ignored = [e for e in journal if e["data"].get("decision") == "ignored"]
    assert ignored and ignored[-1]["seq"] == late[0]["seq"]
    assert "history" not in (await _instance(client, key, instance["id"]))["data"]
    assert not await _events(client, key, "process.recall_completed")


async def test_a_request_memory_rejects_is_the_steps_timeout_at_once(
    client: httpx.AsyncClient, worker: Worker, memory: CaseMemory
) -> None:
    s = await _setup(client)
    memory.rejected = True
    await _observe(client, s["key"], "sample.opened", number="S-1", amount=1)
    await worker.run_once()
    [recall] = await _recalls(worker)
    assert recall.state == "answered"
    [timed_out] = await _events(client, s["key"], "process.recall_timed_out")
    assert timed_out["payload"]["reason"] == "memory_rejected"
    [instance] = await _instances(client, s["key"], definitionKey="sample-memory")
    assert instance["data"]["note"] == "no memory"


async def test_a_recall_the_step_no_longer_waits_for_is_closed(
    client: httpx.AsyncClient, worker: Worker, memory: CaseMemory, sync_engine: Engine
) -> None:
    s = await _setup(client)
    memory.fail = "typed"
    await _observe(client, s["key"], "sample.opened", number="S-1", amount=1)
    await worker.run_once()
    [instance] = await _instances(client, s["key"], definitionKey="sample-memory")
    response = await client.post(
        f"/api/v1/process-instances/{instance['id']}:cancel",
        json={"reason": "not needed"},
        headers=auth(s["key"]),
    )
    assert response.status_code == 200, response.text
    await worker.run_once()
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE process_recalls SET next_attempt_at = now()"))
    await worker.run_once()
    [recall] = await _recalls(worker)
    assert recall.state == "closed"
    assert recall.attempts == 1, "memory is not asked for a step that no longer waits"


async def test_the_pack_of_a_step_is_recorded_with_its_read_by_similarity(
    client: httpx.AsyncClient, worker: Worker, memory: CaseMemory
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _observe(client, key, "sample.opened", number="S-1", amount=1)
    await worker.run_once()
    [instance] = await _instances(client, key, definitionKey="sample-memory")
    task_id = _open(instance, "review")["taskId"]
    session = await open_session(client, key)
    claimed = await claim_task(client, key, task_id, session["id"])
    assert claimed.status_code in (200, 201), claimed.text

    async def read() -> dict[str, Any]:
        response = await client.post(
            "/api/v1/context", json={"task": task_id, "query": "continue"}, headers=auth(key)
        )
        assert response.status_code == 200, response.text
        context: dict[str, Any] = response.json()["taskContext"]
        return context

    first = await read()
    assert first["recorded"] is True and first["replayed"] is False
    memory.nodes.pop("lesson:1")  # the graph changes; the claim keeps its pack's request
    again = await read()
    assert again["replayed"] is True
    assert again["contextPackId"] == first["contextPackId"]
    assert [i["natural_key"] for i in again["pack"]["sections"][-1]["items"]] == ["lesson:similar"]
    assert again["pack"]["sections"][-1]["inferred"] is True
    recorded = await client.get(
        f"/api/v1/context-packs/{first['contextPackId']}", headers=auth(key)
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["request"]["semantic"]["allow_semantic"] is True
    assert recorded.json()["anchors"] == [{"value": CASE, "kind": "case", "source": "step"}]
    assert "lesson:similar" in {e["natural_key"] for e in recorded.json()["used"]["entities"]}


# --- the retrospective of a closed case (CP-ADR-0076 §6, §8; P027) --------------------------


async def test_a_retrospective_asks_its_skill_by_the_contract_with_the_journal(
    client: httpx.AsyncClient, worker: Worker, memory: CaseMemory
) -> None:
    s = await _setup(client)
    key = s["key"]
    contract = retrospective_contract()
    responses = [
        await client.post(
            "/api/v1/skills",
            json={
                "name": "process.retrospective",
                "version": "1",
                "sideEffects": "none",
                "riskLevel": "low",
                "contract": {
                    "inputs": contract["inputs"],
                    "outputs": contract["outputs"],
                    "implementation": contract["implementation"],
                },
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/task-types",
            json={
                "key": "lessons-review",
                "displayName": "Lessons",
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
            },
            headers=auth(key),
        ),
    ]
    for response in responses:
        assert response.status_code in (200, 201), response.text
    spec = _process(s["admin"], s["workspace"])
    spec["stages"] = [{"id": "work", "steps": [{"id": "done", "complete": {"outcome": "closed"}}]}]
    spec["retrospective"] = {
        "taskType": "lessons-review",
        "assign": [{"principal": s["admin"]}],
        "appliesTo": ["case"],
    }
    await _publish(client, key, "sample-retro", spec)

    await _observe(client, key, "sample.opened", number="S-9", amount=10)
    await worker.run_once()
    [instance] = await _instances(client, key, definitionKey="sample-retro")
    assert instance["status"] == "completed"
    journal = await _journal(client, key, instance["id"])
    [asked] = [e for e in journal if e["kind"] == "intent" and e["reason"] == "invoke_skill"]
    assert asked["data"]["executed"]["ok"] is True, asked["data"]["executed"]
    assert "journal" not in asked["data"]["input"], "the journal is not recorded in itself"

    async with transaction(worker.session_factory) as session:
        [invocation] = (await session.scalars(select(SkillInvocation))).all()
    inputs = invocation.inputs
    errors = [
        e.message for e in jsonschema.Draft202012Validator(contract["inputs"]).iter_errors(inputs)
    ]
    assert not errors, errors
    assert inputs["case"] == {"kind": "case", "key": "sample:S-9", "title": None}
    assert (inputs["definitionKey"], inputs["appliesToKinds"]) == ("sample-retro", ["case"])
    # The step that closed the case is read before its record is stored.
    seqs = {entry["seq"] for entry in inputs["journal"]}
    assert seqs == {e["seq"] for e in journal}
    assert any(e["reason"] == "completed" for e in inputs["journal"])
