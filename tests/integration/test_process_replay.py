"""Replay and the trial run on Postgres (CP-ADR-0074 §10; process-packages P014).

The acceptance of P014 (SC-005): the published version replayed on the
journals of its own instances gives zero divergences; a candidate with one
changed row of a decision table diverges exactly the instances that row
decides otherwise. ``given.fromInstance`` of a package test goes on from a
copy of a live instance. Neither writes anything: every table of the
database is the same, row for row, after the call as before it.
"""

import copy
from typing import Any

import httpx
import yaml
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key
from tests.integration.test_package_test import API_VERSION, snapshot
from tests.integration.test_process_instances import AGENT, _publish, _setup

PROCESS = "sample-replay"


def _spec(admin: str, *, version: int = 1, low: str = "[0..1000)", high: str = "[1000..100000)"):
    return {
        "version": version,
        "displayName": "Sample replay",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "level": {"type": "number"},
                "decision": {"type": "string"},
            },
        },
        "start": {"on": {"observation": "sample.opened"}, "key": "event.payload.data.number"},
        "decisions": [
            {
                "id": "level",
                "hitPolicy": "first",
                "inputs": [{"id": "amount", "expr": "data.amount", "type": "number"}],
                "outputs": [{"id": "level", "type": "number"}],
                "rules": [
                    {"when": {"amount": low}, "then": {"level": 1}},
                    {"when": {"amount": high}, "then": {"level": 2}},
                ],
            }
        ],
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "choose",
                        "decide": {"table": "level"},
                        "output": {"as": {"level": "step.result.level"}},
                    },
                    {
                        "id": "review",
                        "human": {"taskType": "review", "assign": [{"principal": admin}]},
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {"id": "done", "complete": {"outcome": "reviewed"}},
                ],
            }
        ],
    }


async def _start(client: httpx.AsyncClient, key: str, number: str, amount: int) -> str:
    response = await client.post(
        "/api/v1/process-instances",
        json={"process": PROCESS, "key": number, "data": {"amount": amount}},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    instance_id: str = response.json()["id"]
    return instance_id


async def _replay(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/process-definitions/{PROCESS}:replay", json=body, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    out: dict[str, Any] = response.json()
    return out


async def test_replay_shows_zero_divergences_on_its_own_journal_and_exactly_the_affected(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _publish(client, key, PROCESS, _spec(admin))
    started = {
        amount: await _start(client, key, f"N-{amount}", amount) for amount in (100, 500, 700)
    }
    before = snapshot(sync_engine)

    # The version on its own journal, and the same behaviour as a next version: nothing differs.
    for candidate in (_spec(admin), _spec(admin, version=2)):
        same = await _replay(client, key, {"spec": candidate})
        assert (same["key"], same["replayed"], same["diverged"]) == (PROCESS, 3, 0), same
        assert same["candidateHash"].startswith("sha256:")
        assert {i["instanceId"] for i in same["instances"]} == set(started.values())
        assert all(i["divergences"] == [] and i["events"] >= 1 for i in same["instances"])
        assert all(i["version"] == 1 for i in same["instances"])

    # One row moved: 500 and 700 fall into the second row now, 100 stays in the first.
    changed = await _replay(
        client, key, {"spec": _spec(admin, version=2, low="[0..400)", high="[400..100000)")}
    )
    assert (changed["replayed"], changed["diverged"]) == (3, 2), changed
    diverged = {i["instanceId"]: i["divergences"] for i in changed["instances"] if i["divergences"]}
    assert set(diverged) == {started[500], started[700]}
    [divergence] = diverged[started[500]]
    # The start is the first entry of the journal: the table decides there.
    assert (divergence["journalSeq"], divergence["kind"], divergence["element"]) == (
        0,
        "decision",
        "choose",
    )
    assert divergence["recorded"]["rules"] == [0] and divergence["replayed"]["rules"] == [1]
    assert snapshot(sync_engine) == before, "a replay writes nothing"

    # instanceIds picks the instances; limit the latest ones.
    picked = await _replay(client, key, {"spec": _spec(admin), "instanceIds": [started[100]]})
    assert [i["instanceId"] for i in picked["instances"]] == [started[100]]
    latest = await _replay(client, key, {"spec": _spec(admin), "limit": 2})
    assert [i["instanceId"] for i in latest["instances"]] == [started[700], started[500]]


async def test_replay_of_an_invalid_candidate_reports_problems_and_replays_nothing(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _publish(client, key, PROCESS, _spec(admin))
    await _start(client, key, "N-1", 100)
    broken = copy.deepcopy(_spec(admin, version=2))
    broken["stages"][0]["steps"][0]["decide"]["table"] = "nothing"
    body = await _replay(client, key, {"spec": broken})
    assert (body["replayed"], body["diverged"], body["instances"]) == (0, 0, [])
    assert any(p["severity"] == "error" for p in body["problems"]), body["problems"]


async def test_replay_needs_its_permission_a_known_process_and_its_instances(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _publish(client, key, PROCESS, _spec(admin))
    _, reader = await create_agent_with_key(
        client, key, name="reader", permissions=["processes.read"]
    )
    path = f"/api/v1/process-definitions/{PROCESS}:replay"
    denied = await client.post(path, json={"spec": _spec(admin)}, headers=auth(reader))
    assert denied.status_code == 403, denied.text
    unknown = await client.post(
        "/api/v1/process-definitions/nothing:replay", json={"spec": _spec(admin)}, headers=auth(key)
    )
    assert unknown.status_code == 404, unknown.text
    stranger = await client.post(
        path,
        json={"spec": _spec(admin), "instanceIds": ["00000000-0000-0000-0000-000000000001"]},
        headers=auth(key),
    )
    assert stranger.status_code == 404, stranger.text


def _package(admin: str, test: dict[str, Any]) -> dict[str, Any]:
    process = {"apiVersion": API_VERSION, "kind": "Process", "key": PROCESS, "spec": _spec(admin)}
    manifest = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "sample-replay",
        "spec": {"version": "1.0.0", "displayName": "Sample replay"},
    }
    files = [
        ("package.yaml", yaml.safe_dump(manifest)),
        ("processes/replay.yaml", yaml.safe_dump(process)),
        ("tests/trial.test.yaml", yaml.safe_dump(test)),
    ]
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def test_a_trial_run_goes_on_from_a_live_instance_and_writes_nothing(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _publish(client, key, PROCESS, _spec(admin))
    instance_id = await _start(client, key, "N-1", 500)
    test = {
        "process": PROCESS,
        "name": "the live case, reviewed",
        "given": {"fromInstance": instance_id},
        "steps": [
            {
                "expect": {
                    "status": "running",
                    "data": {"amount": 500, "level": 1},
                    "tasks": [{"step": "review", "assignee": admin, "status": "open"}],
                }
            },
            {"complete": {"step": "review", "by": admin, "output": {"decision": "yes"}}},
            {
                "expect": {
                    "status": "completed",
                    "outcome": "reviewed",
                    "data": {"decision": "yes"},
                    "noSideEffects": True,
                }
            },
        ],
    }
    before = snapshot(sync_engine)
    response = await client.post(
        "/api/v1/packages:test", json={"package": _package(admin, test)}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "passed", body
    assert snapshot(sync_engine) == before
    live = await client.get(f"/api/v1/process-instances/{instance_id}", headers=auth(key))
    assert live.json()["status"] == "running", "the live instance is not touched"

    # An unknown instance is 404, as GET /process-instances/{id}.
    test["given"] = {"fromInstance": "00000000-0000-0000-0000-000000000001"}
    missing = await client.post(
        "/api/v1/packages:test", json={"package": _package(admin, test)}, headers=auth(key)
    )
    assert missing.status_code == 404, missing.text
