"""Case and process projection through the Context Adapter on Postgres (P011).

CP-ADR-0076 §2-§3 end to end: a published process and a case of it, driven
through the public API and the worker, reach memory through the adapter's
own cursor — the version as process, stage and step nodes, the case as its
node, facts, entities and documents. Delivering the same journal again does
not change the graph; a change of the data closes the fact it replaces.
The graph is ``ProjectionMemory``, the projection rules of memory-service.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.context_adapter import CONSUMER_NAME, ContextAdapter
from control_plane.worker.main import Worker
from tests.fake_projection_memory import ProjectionMemory
from tests.helpers import auth
from tests.integration.test_process_instances import (
    AGENT,
    _complete,
    _instances,
    _observe,
    _publish,
    _setup,
)

CASE = "case:sample:S-1"
PROCESS = "process:sample-case"


def _case(admin: str) -> dict[str, Any]:
    person = [{"principal": admin}]
    return {
        "version": 1,
        "displayName": "Sample case",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "governedBy": [{"document": "regulation:charter"}],
        "data": {
            "type": "object",
            "properties": {
                "number": {"type": "string"},
                "amount": {"type": "number"},
                "party": {"type": "string"},
                "secret": {"type": "string"},
            },
        },
        "start": {
            "on": {"observation": "sample.opened"},
            "key": "event.payload.data.number",
            "set": {
                "number": "string(event.payload.data.number)",
                "amount": "double(event.payload.data.amount)",
                "party": "string(event.payload.data.party)",
                "secret": "string(event.payload.data.secret)",
            },
        },
        "correlate": [
            {
                "on": {"observation": "sample.moved"},
                "key": "event.payload.data.number",
                "set": {"amount": "double(event.payload.data.amount)"},
            }
        ],
        "memory": {
            "case": {"key": "'sample:' + data.number", "title": "'Sample ' + data.number"},
            "facts": {"amount": "data.amount"},
            "entities": [{"kind": "party", "key": "data.party", "rel": "counterpart"}],
            "documents": {"artifacts": ["sample-document"]},
        },
        "stages": [
            {
                "id": "work",
                "displayName": "Work",
                "steps": [
                    {
                        "id": "review",
                        "human": {"taskType": "review", "assign": person},
                        "governedBy": [{"document": "regulation:handbook", "section": "2"}],
                    },
                    {"id": "done", "complete": {"outcome": "reviewed"}},
                ],
            }
        ],
    }


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


@pytest.fixture
def delivered(settings: Settings, app: Any) -> tuple[ContextAdapter, ProjectionMemory]:
    memory = ProjectionMemory()
    adapter = ContextAdapter(settings, engine=app.state.engine, provider=memory)
    return adapter, memory


async def _drain(adapter: ContextAdapter) -> None:
    while await adapter.deliver_once():
        pass


def _rewind(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0 WHERE name = :n"),
            {"n": CONSUMER_NAME},
        )


async def _review_task(client: httpx.AsyncClient, key: str) -> str:
    [instance] = await _instances(client, key, definitionKey="sample-case")
    response = await client.get(f"/api/v1/process-instances/{instance['id']}", headers=auth(key))
    [element] = [e for e in response.json()["openElements"] if e["id"] == "review"]
    task_id: str = element["taskId"]
    return task_id


async def test_case_and_version_reach_the_graph_once_and_keep_their_history(
    client: httpx.AsyncClient,
    worker: Worker,
    delivered: tuple[ContextAdapter, ProjectionMemory],
    sync_engine: Engine,
) -> None:
    adapter, memory = delivered
    s = await _setup(client)
    key = s["key"]
    created = await client.post(
        "/api/v1/artifact-types",
        json={
            "key": "sample-document",
            "displayName": "Sample document",
            "mediaTypes": ["text/*"],
        },
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    await _publish(client, key, "sample-case", _case(s["admin"]))
    await _observe(
        client, key, "sample.opened", number="S-1", amount=100, party="P-7", secret="hidden"
    )
    await worker.run_once()
    await _drain(adapter)

    # The version: process, stage and step, with the relations of the ontology.
    assert memory.nodes[PROCESS]["type"] == "process"
    assert memory.nodes[f"{PROCESS}/work"]["type"] == "process_stage"
    assert memory.nodes[f"{PROCESS}/review"]["type"] == "process_step"
    assert {(s_, p, o) for s_, p, o, _ in memory.edges(predicate="step_of")} == {
        (f"{PROCESS}/review", "step_of", f"{PROCESS}/work"),
        (f"{PROCESS}/done", "step_of", f"{PROCESS}/work"),
    }
    assert {(s_, o) for s_, _, o, _ in memory.edges(predicate="regulates")} == {
        ("regulation:charter", PROCESS),
        ("regulation:handbook", f"{PROCESS}/review"),
    }
    # The case: its node, its fact, its party and its process.
    assert memory.nodes[CASE]["title"] == "Sample S-1"
    assert memory.nodes[CASE]["properties"] == {"amount": 100}
    assert {p for _, p, _, _ in memory.edges(CASE)} == {
        "instance_of",
        "has_fact",
        "counterpart",
    }
    assert (CASE, "counterpart", "party:P-7") in {k[:3] for k in memory.edges(CASE)}
    # Only the projection: the instance data it does not declare never leaves the core.
    projected = [o for o in memory.observations.values() if o["kind"].startswith("process.")]
    assert projected and "hidden" not in repr(projected)

    # A document of the case: an artifact of the declared type on the case's task.
    task_id = await _review_task(client, key)
    artifact = await client.post(
        "/api/v1/artifacts",
        json={
            "task": task_id,
            "type": "sample-document",
            "name": "Notice",
            "content": {"text": "The notice of the case."},
        },
        headers=auth(key),
    )
    assert artifact.status_code == 201, artifact.text
    other = await client.post(
        "/api/v1/artifacts",
        json={"task": task_id, "type": "note", "name": "Scratch", "content": {"text": "x"}},
        headers=auth(key),
    )
    assert other.status_code == 201, other.text
    await _drain(adapter)
    document = f"document:artifact/{artifact.json()['id']}"
    assert list(memory.documents) == [document]  # the undeclared type stays out
    assert "The notice of the case." in memory.documents[document]["chunks"][0]["text"]
    assert [k[:3] for k in memory.edges(CASE, "has_document")] == [(CASE, "has_document", document)]

    # A change of the data: the new amount opens a fact, the old one closes.
    await _observe(client, key, "sample.moved", number="S-1", amount=120)
    await worker.run_once()
    await _drain(adapter)
    amounts = sorted(
        (memory.nodes[o]["properties"]["value"], since, value["valid_to"])
        for (s_, p, o, since), value in memory.facts.items()
        if s_ == CASE and p == "has_fact"
    )
    assert [a[0] for a in amounts] == [100, 120]
    assert amounts[0][2] is not None and amounts[0][2] == amounts[1][1]
    assert amounts[1][2] is None
    assert memory.nodes[CASE]["properties"] == {"amount": 120}

    await _complete(client, key, task_id, {"decision": "ok"})
    await worker.run_once()
    await _drain(adapter)
    [instance] = await _instances(client, key, definitionKey="sample-case")
    assert instance["status"] == "completed"

    # Delivered again from the origin: the graph does not change.
    before = memory.graph()
    documents = dict(memory.documents)
    _rewind(sync_engine)
    await _drain(adapter)
    assert memory.graph() == before
    assert memory.documents == documents

    # A memory rebuilt from the journal alone gets the same graph.
    rebuilt = ProjectionMemory()
    adapter.provider = rebuilt  # type: ignore[assignment]
    _rewind(sync_engine)
    await _drain(adapter)
    assert rebuilt.graph() == before
