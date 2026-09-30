"""Lock order between ``principals/{id}:disable`` and the principal's own work (CP-ADR-0077 §3).

``:disable`` locks the principal first, its sessions later, then tasks and
claims, and its skill call rows last. Whatever the principal does meanwhile must
take the same rows in the same order — including the ``FOR KEY SHARE`` a
foreign-key check takes on the principal when a claim or a run referencing it is
inserted — or a heartbeat and the disable of its executor, a ``:complete`` and
the disable of the call's authority, a claim or a run start and the disable of
the agent doing it wait on each other.
"""

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)
from tests.integration.test_approval_outcomes import _decide, _review_setup
from tests.integration.test_process_instances import AGENT, ISSUER
from tests.integration.test_process_instances import _publish as publish_process
from tests.integration.test_process_instances import _setup as process_setup
from tests.integration.test_skill_invocations_m21 import (
    CALLER_PERMISSIONS,
    EXECUTOR_PERMISSIONS,
    invoke,
    published,
)

RUNNER_PERMISSIONS = ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]


@contextmanager
def _row_locked_for_update(sync_engine: Engine, table: str, row_id: str) -> Iterator[int]:
    """Hold a row the way ``:disable`` does until the block ends; yield the holder's pid."""
    with sync_engine.connect() as conn:
        conn.execute(text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"), {"id": row_id})
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        try:
            yield pid
        finally:
            conn.rollback()


async def _wait_until_blocked_by(sync_engine: Engine, holder_pid: int) -> None:
    """Wait until some backend waits on a lock held by ``holder_pid``."""
    for _ in range(100):
        with sync_engine.connect() as conn:
            waiting = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND :holder = ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the request never waited on the held row")


def _blocked_backend(sync_engine: Engine, holder_pid: int) -> int:
    with sync_engine.connect() as conn:
        pid: int = conn.execute(
            text(
                "SELECT pid FROM pg_stat_activity "
                "WHERE datname = current_database() AND :holder = ANY(pg_blocking_pids(pid))"
            ),
            {"holder": holder_pid},
        ).scalar_one()
    return pid


def _assert_touched_no_task_row(sync_engine: Engine, pid: int, what: str) -> None:
    """The backend has locked, inserted or updated no task row in its transaction.

    Row locks are not listed one by one, but the table lock a row lock or a
    write takes (``RowShareLock``/``RowExclusiveLock``) stays until the end of
    the transaction — savepoints released or not.
    """
    with sync_engine.connect() as conn:
        modes = conn.execute(
            text(
                "SELECT mode FROM pg_locks WHERE pid = :pid AND relation = 'tasks'::regclass "
                "AND mode IN ('RowShareLock', 'RowExclusiveLock')"
            ),
            {"pid": pid},
        ).all()
    assert not modes, what


def _assert_free(sync_engine: Engine, table: str, row_id: str, what: str) -> None:
    with sync_engine.connect() as conn:
        try:
            conn.execute(
                text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE NOWAIT"), {"id": row_id}
            )
        except OperationalError as exc:  # pragma: no cover - the regression
            raise AssertionError(what) from exc
        finally:
            conn.rollback()


async def test_a_heartbeat_takes_its_session_before_its_call_row(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, caller_key = await create_agent_with_key(
        client, admin_key, name="caller", permissions=CALLER_PERMISSIONS
    )
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=EXECUTOR_PERMISSIONS
    )
    await published(client, admin_key)
    created = (await invoke(client, caller_key, "repo.search", {"query": "x"})).json()
    work_session = await open_session(client, executor_key)
    claimed = await client.post(
        "/api/v1/skill-invocations:claim",
        json={
            "protocols": ["local"],
            "localEntrypoints": ["cp_skills.search:run"],
            "sessionId": work_session["id"],
        },
        headers=auth(executor_key),
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["invocation"]
    assert lease["id"] == created["id"]

    with _row_locked_for_update(sync_engine, "sessions", work_session["id"]) as holder:
        heartbeat = asyncio.create_task(
            client.post(
                f"/api/v1/skill-invocations/{lease['id']}:heartbeat",
                json={"fencingToken": lease["fencingToken"], "sessionId": work_session["id"]},
                headers=auth(executor_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        # The heartbeat waits on the session and holds nothing on the call row:
        # the disable, next in line for that row, is not blocked by it.
        _assert_free(
            sync_engine, "skill_invocations", lease["id"], "the heartbeat locked the call row first"
        )

    response = await heartbeat
    assert response.status_code == 200, response.text


async def test_a_complete_on_a_task_takes_its_authority_before_its_session_and_call_row(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    caller, caller_key = await create_agent_with_key(
        client, admin_key, name="caller", permissions=CALLER_PERMISSIONS
    )
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=EXECUTOR_PERMISSIONS
    )
    await published(client, admin_key)
    task = await create_task(client, admin_key, title="Search")
    created = await invoke(
        client, caller_key, "repo.search", {"query": "x"}, taskId=task["publicId"]
    )
    assert created.status_code == 201, created.text
    work_session = await open_session(client, executor_key)
    claimed = await client.post(
        "/api/v1/skill-invocations:claim",
        json={
            "protocols": ["local"],
            "localEntrypoints": ["cp_skills.search:run"],
            "sessionId": work_session["id"],
        },
        headers=auth(executor_key),
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["invocation"]
    assert lease["id"] == created.json()["id"]

    # The result artifact is authored by the caller and points at the task:
    # ``:disable`` of the caller holds the principal (its first lock) and later
    # the task, and waits for the call row last.
    with _row_locked_for_update(sync_engine, "principals", caller["id"]) as holder:
        complete = asyncio.create_task(
            client.post(
                f"/api/v1/skill-invocations/{lease['id']}:complete",
                json={
                    "fencingToken": lease["fencingToken"],
                    "output": {"hits": 1},
                    "sessionId": work_session["id"],
                },
                headers=auth(executor_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        # The complete waits on its authority and holds nothing the disable
        # takes after the principal.
        _assert_free(
            sync_engine, "sessions", work_session["id"], "the complete locked its session first"
        )
        _assert_free(
            sync_engine, "skill_invocations", lease["id"], "the complete locked the call row first"
        )
        _assert_free(sync_engine, "tasks", task["id"], "the complete locked the task first")

    response = await complete
    assert response.status_code == 200, response.text
    assert response.json()["artifactId"] is not None


async def test_a_claim_takes_its_principal_before_its_session_and_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key, name="worker")
    task = await create_task(client, admin_key, title="Work")
    work_session = await open_session(client, agent_key)

    # The claim names the agent as its holder: its foreign-key check would take
    # the principal after the session and the task, which ``:disable`` of the
    # agent takes after the principal.
    with _row_locked_for_update(sync_engine, "principals", agent["id"]) as holder:
        claim = asyncio.create_task(claim_task(client, agent_key, task["id"], work_session["id"]))
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(
            sync_engine, "sessions", work_session["id"], "the claim locked its session first"
        )
        _assert_free(sync_engine, "tasks", task["id"], "the claim locked the task first")

    response = await claim
    assert response.status_code == 200, response.text


async def test_a_takeover_takes_its_principal_before_its_session_and_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, holder_key = await create_agent_with_key(client, admin_key, name="holder")
    taker, taker_key = await create_agent_with_key(client, admin_key, name="taker")
    task = await create_task(client, admin_key, title="Work")
    holder_session = await open_session(client, holder_key)
    claimed = await claim_task(client, holder_key, task["id"], holder_session["id"])
    assert claimed.status_code == 200, claimed.text
    taker_session = await open_session(client, taker_key)

    with _row_locked_for_update(sync_engine, "principals", taker["id"]) as holder:
        takeover = asyncio.create_task(
            client.post(
                f"/api/v1/claims/{claimed.json()['id']}:reclaim",
                json={"sessionId": taker_session["id"]},
                headers=auth(taker_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(
            sync_engine, "sessions", taker_session["id"], "the takeover locked its session first"
        )
        _assert_free(sync_engine, "tasks", task["id"], "the takeover locked the task first")

    # The lease is still live: once let through, the takeover is refused.
    response = await takeover
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "claim_not_expired"


async def _run_on_behalf_of_a_human(
    client: httpx.AsyncClient, admin_key: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """An agent claims a task in a session opened on behalf of a human.

    Returns (human, task, session, claim, agent_key).
    """
    human, _ = await create_agent_with_key(client, admin_key, name="alice", kind="human")
    agent, agent_key = await create_agent_with_key(
        client, admin_key, name="worker", permissions=RUNNER_PERMISSIONS
    )
    delegated = await client.post(
        "/api/v1/delegations",
        json={
            "humanPrincipalId": human["id"],
            "agentPrincipalId": agent["id"],
            "permissions": ["tasks.write"],
        },
        headers=auth(admin_key),
    )
    assert delegated.status_code == 201, delegated.text
    task = await create_task(client, admin_key, title="Work")
    work_session = await open_session(client, agent_key, onBehalfOf=human["id"])
    claimed = await claim_task(client, agent_key, task["id"], work_session["id"])
    assert claimed.status_code == 200, claimed.text
    return human, task, work_session, claimed.json(), agent_key


async def test_a_run_start_on_behalf_of_a_human_takes_the_session_before_the_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """B1: the run references the claim's session, which ``:disable`` of the
    human closes after locking the human and before the task."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, task, work_session, claim, agent_key = await _run_on_behalf_of_a_human(client, admin_key)

    with _row_locked_for_update(sync_engine, "sessions", work_session["id"]) as holder:
        start = asyncio.create_task(
            client.post(
                f"/api/v1/tasks/{task['id']}:start-run",
                json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
                headers=auth(agent_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(sync_engine, "tasks", task["id"], "the run start locked the task first")

    response = await start
    assert response.status_code == 201, response.text


async def test_a_checkpoint_takes_the_run_session_before_the_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """B2: a checkpoint (and a run action) references the run's session and
    principal; ``:disable`` of the human holds the session before the task."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, task, work_session, claim, agent_key = await _run_on_behalf_of_a_human(client, admin_key)
    started = await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert started.status_code == 201, started.text
    run = started.json()

    with _row_locked_for_update(sync_engine, "sessions", work_session["id"]) as holder:
        checkpoint = asyncio.create_task(
            client.post(
                f"/api/v1/runs/{run['id']}/checkpoints",
                json={"kind": "progress", "data": {"step": 1}},
                headers=auth(agent_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(sync_engine, "tasks", task["id"], "the checkpoint locked the task first")
        _assert_free(sync_engine, "runs", run["id"], "the checkpoint locked the run first")

    response = await checkpoint
    assert response.status_code == 201, response.text


async def test_a_run_succeed_takes_its_principal_and_session_before_the_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """B3: ``:succeed`` completes the task and writes the completion work and
    the verification attempt authored by the caller — after the task."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key, name="worker")
    task = await create_task(client, admin_key, title="Work")
    work_session = await open_session(client, agent_key)
    claimed = await claim_task(client, agent_key, task["id"], work_session["id"])
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    started = await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert started.status_code == 201, started.text
    run = started.json()

    with _row_locked_for_update(sync_engine, "principals", agent["id"]) as holder:
        succeed = asyncio.create_task(
            client.post(f"/api/v1/runs/{run['id']}:succeed", json={}, headers=auth(agent_key))
        )
        await _wait_until_blocked_by(sync_engine, holder)
        # Blocked on the write flow's first statement: nothing else is held.
        _assert_free(sync_engine, "sessions", work_session["id"], "succeed locked the session")
        _assert_free(sync_engine, "tasks", task["id"], "succeed locked the task first")
        _assert_free(sync_engine, "runs", run["id"], "succeed locked the run first")

    response = await succeed
    assert response.status_code == 200, response.text

    # And the session goes before the task too (a session on behalf of someone
    # else is not covered by the caller's lock).
    other = await create_task(client, admin_key, title="More work")
    claimed = await claim_task(client, agent_key, other["id"], work_session["id"])
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    started = await client.post(
        f"/api/v1/tasks/{other['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert started.status_code == 201, started.text
    with _row_locked_for_update(sync_engine, "sessions", work_session["id"]) as holder:
        succeed = asyncio.create_task(
            client.post(
                f"/api/v1/runs/{started.json()['id']}:succeed", json={}, headers=auth(agent_key)
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(sync_engine, "tasks", other["id"], "succeed locked the task first")

    response = await succeed
    assert response.status_code == 200, response.text


async def test_a_complete_takes_the_claim_session_before_the_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """B3 for ``tasks/{id}:complete`` under a claim held on behalf of a human."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, task, work_session, claim, agent_key = await _run_on_behalf_of_a_human(client, admin_key)
    current = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))
    assert current.status_code == 200, current.text

    with _row_locked_for_update(sync_engine, "sessions", work_session["id"]) as holder:
        complete = asyncio.create_task(
            client.post(
                f"/api/v1/tasks/{task['id']}:complete",
                json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
                headers={**auth(agent_key), "If-Match": f'"task-{current.json()["version"]}"'},
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(sync_engine, "tasks", task["id"], "the complete locked the task first")

    response = await complete
    assert response.status_code == 200, response.text


async def test_an_executor_claim_takes_its_principal_before_its_session_and_the_call(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """B7: taking a call writes ``executor_principal_id`` — a foreign key to
    the executor, checked after the session and the call row."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, caller_key = await create_agent_with_key(
        client, admin_key, name="caller", permissions=CALLER_PERMISSIONS
    )
    executor, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=EXECUTOR_PERMISSIONS
    )
    await published(client, admin_key)
    created = (await invoke(client, caller_key, "repo.search", {"query": "x"})).json()
    work_session = await open_session(client, executor_key)

    with _row_locked_for_update(sync_engine, "principals", executor["id"]) as holder:
        claim = asyncio.create_task(
            client.post(
                "/api/v1/skill-invocations:claim",
                json={
                    "protocols": ["local"],
                    "localEntrypoints": ["cp_skills.search:run"],
                    "sessionId": work_session["id"],
                },
                headers=auth(executor_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(
            sync_engine, "sessions", work_session["id"], "the claim locked its session first"
        )
        _assert_free(
            sync_engine, "skill_invocations", created["id"], "the claim locked the call first"
        )

    response = await claim
    assert response.status_code == 200, response.text
    assert response.json()["invocation"]["id"] == created["id"]


async def test_every_write_takes_its_caller_first(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The write flow's first statement: the caller's principal, before the
    task a plain ``PATCH`` locks — whatever the command does next."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key, name="worker")
    task = await create_task(client, admin_key, title="Work")

    with _row_locked_for_update(sync_engine, "principals", agent["id"]) as holder:
        patch = asyncio.create_task(
            client.patch(
                f"/api/v1/tasks/{task['id']}",
                json={"title": "Renamed"},
                headers={**auth(agent_key), "If-Match": f'"task-{task["version"]}"'},
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(sync_engine, "tasks", task["id"], "the update locked the task first")

    response = await patch
    assert response.status_code == 200, response.text


async def test_a_write_in_flight_when_its_caller_is_disabled_is_refused(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Auth read the principal before the transaction; the caller's lock reads
    it again after ``:disable`` commits: no session opens for a disabled agent."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key, name="worker")

    with sync_engine.connect() as conn:
        conn.execute(
            text("SELECT id FROM principals WHERE id = :id FOR UPDATE"), {"id": agent["id"]}
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        opening = asyncio.create_task(
            client.post("/api/v1/sessions", json={"clientName": "late"}, headers=auth(agent_key))
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :id"), {"id": agent["id"]}
        )
        conn.commit()

    response = await opening
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "principal_not_active"
    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM sessions WHERE principal_id = :id"), {"id": agent["id"]}
        ).scalar_one()
    assert count == 0


async def test_two_admins_disabling_each_other_do_not_deadlock(
    client: httpx.AsyncClient,
) -> None:
    """``:disable`` takes the caller together with the target in id order: the
    two calls meet on the lower id instead of each holding its own caller."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    first, first_key = await create_agent_with_key(
        client, admin_key, name="ann", kind="human", permissions=["admin"]
    )
    second, second_key = await create_agent_with_key(
        client, admin_key, name="bob", kind="human", permissions=["admin"]
    )

    responses = await asyncio.gather(
        client.post(f"/api/v1/principals/{second['id']}:disable", headers=auth(first_key)),
        client.post(f"/api/v1/principals/{first['id']}:disable", headers=auth(second_key)),
    )
    # One of them wins; the other finds its own caller disabled.
    assert sorted(r.status_code for r in responses) == [200, 403], [r.text for r in responses]
    refused = next(r for r in responses if r.status_code == 403)
    assert refused.json()["error"]["code"] == "principal_not_active"


async def test_a_run_start_takes_its_principal_before_the_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key, name="worker")
    task = await create_task(client, admin_key, title="Work")
    work_session = await open_session(client, agent_key)
    claimed = await claim_task(client, agent_key, task["id"], work_session["id"])
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()

    # The run references the agent and its session: ``:disable`` of the agent
    # holds the principal, then the session, then the task.
    with _row_locked_for_update(sync_engine, "principals", agent["id"]) as holder:
        start = asyncio.create_task(
            client.post(
                f"/api/v1/tasks/{task['id']}:start-run",
                json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
                headers=auth(agent_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        _assert_free(sync_engine, "tasks", task["id"], "the run start locked the task first")

    response = await start
    assert response.status_code == 201, response.text


RULE_ACTION: dict[str, Any] = {
    "kind": "ensure_work",
    "taskType": "task",
    "dedupKeyTemplate": "seen:{{payload.data.id}}",
}


async def test_a_rule_batch_takes_every_rule_authority_before_its_first_task(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """One journal batch, two rules with different authorities: the first files
    work (a task row, held to the commit), the second acts as the human.
    ``:disable`` of the human holds the human and may wait for that task."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human, human_key = await create_agent_with_key(
        client,
        admin_key,
        name="alice",
        kind="human",
        permissions=["rules.write", "rules.read", "tasks.write", "tasks.read", "events.read"],
    )
    for key, rule_key, title in (
        (admin_key, "first", "By admin"),
        (human_key, "second", "By human"),
    ):
        created = await client.post(
            "/api/v1/rules",
            json={
                "key": rule_key,
                "trigger": {"kind": "observation", "type": "thing.seen"},
                "action": {
                    **RULE_ACTION,
                    "dedupKeyTemplate": f"{rule_key}:{{{{payload.data.id}}}}",
                    "fields": {"title": title},
                },
            },
            headers=auth(key),
        )
        assert created.status_code == 201, created.text
    observed = await client.post(
        "/api/v1/observations",
        json={"kind": "thing.seen", "content": "seen", "data": {"id": 1}},
        headers=auth(admin_key),
    )
    assert observed.status_code == 201, observed.text

    worker = Worker(settings)
    try:
        with _row_locked_for_update(sync_engine, "principals", human["id"]) as holder:
            batch = asyncio.create_task(worker.process_rule_events())
            await _wait_until_blocked_by(sync_engine, holder)
            _assert_touched_no_task_row(
                sync_engine,
                _blocked_backend(sync_engine, holder),
                "the batch filed work before taking the human's principal",
            )
        await batch
    finally:
        await worker.engine.dispose()

    with sync_engine.connect() as conn:
        titles = (
            conn.execute(
                text("SELECT title FROM tasks WHERE origin->>'kind' = 'rule' ORDER BY title")
            )
            .scalars()
            .all()
        )
    assert titles == ["By admin", "By human"]


async def test_an_outcome_takes_the_assignee_of_its_work_before_completing_the_task(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """``completeTask`` locks the review; ``ensureWork`` then files work assigned
    to the coder. ``:disable`` of the coder holds the coder first."""
    schema = {
        "gates": {
            "default": {
                "outcomes": {
                    "approved": [
                        {"completeTask": {}},
                        {
                            "ensureWork": {
                                "type": "coding-task",
                                "key": "follow-up:$.approval.id",
                                "title": "Follow up $.spawnedBy.publicId",
                                "assignee": "$.spawnedBy.assigneeId!",
                            }
                        },
                    ]
                }
            }
        }
    }
    s = await _review_setup(client, schema=schema)
    await _decide(client, s["reviewer_key"], s["approval"]["id"], "approve")

    worker = Worker(settings)
    try:
        with _row_locked_for_update(sync_engine, "principals", s["coder"]["id"]) as holder:
            outcomes = asyncio.create_task(worker.process_outcomes())
            await _wait_until_blocked_by(sync_engine, holder)
            _assert_touched_no_task_row(
                sync_engine,
                _blocked_backend(sync_engine, holder),
                "the outcome completed the review before taking the assignee",
            )
        await outcomes
    finally:
        await worker.engine.dispose()

    outcome = await client.get(
        f"/api/v1/approvals/{s['approval']['id']}/outcome", headers=auth(s["admin_key"])
    )
    assert outcome.json()["outcomeStatus"] == "executed", outcome.text
    with sync_engine.connect() as conn:
        assignee = conn.execute(
            text("SELECT assignee_id FROM tasks WHERE title LIKE 'Follow up %'")
        ).scalar_one()
    assert str(assignee) == s["coder"]["id"]


def _one_step_process(observation: str, assign: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "displayName": f"On {observation}",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {"type": "object", "properties": {}},
        "start": {"on": {"observation": observation}, "key": "event.payload.data.n"},
        "stages": [
            {
                "id": "work",
                "steps": [
                    {"id": "review", "human": {"taskType": "review", "assign": assign}},
                    {"id": "done", "complete": {"outcome": "done"}},
                ],
            }
        ],
    }


async def test_a_process_batch_takes_the_principals_its_steps_name_before_its_first_task(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """One journal batch: the first event starts an instance that files a task
    (held to the commit), the second one whose step assigns the human.
    ``:disable`` of the human holds the human and may wait for that task."""
    s = await process_setup(client)
    human, _ = await create_agent_with_key(client, s["key"], name="alice", kind="human")
    await publish_process(
        client, s["key"], "first", _one_step_process("sample.first", [{"principal": s["admin"]}])
    )
    await publish_process(
        client, s["key"], "second", _one_step_process("sample.second", [{"principal": human["id"]}])
    )
    for kind in ("sample.first", "sample.second"):
        observed = await client.post(
            "/api/v1/observations",
            json={"kind": kind, "content": "seen", "data": {"n": "1"}},
            headers=auth(s["key"]),
        )
        assert observed.status_code in (200, 201), observed.text

    worker = Worker(settings)
    try:
        with _row_locked_for_update(sync_engine, "principals", human["id"]) as holder:
            batch = asyncio.create_task(worker.process_events())
            await _wait_until_blocked_by(sync_engine, holder)
            _assert_touched_no_task_row(
                sync_engine,
                _blocked_backend(sync_engine, holder),
                "the process filed work before taking the principal its step names",
            )
        await batch
    finally:
        await worker.engine.dispose()

    with sync_engine.connect() as conn:
        assignees = (
            conn.execute(text("SELECT assignee_id FROM tasks ORDER BY created_at")).scalars().all()
        )
    assert [str(a) for a in assignees] == [s["admin"], human["id"]]


async def test_a_process_batch_takes_the_agent_a_call_step_names_before_its_first_task(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """``call: {agent: X}``: the engine builds the step's assign chain itself;
    the agent's principal is still taken before the batch's first task —
    ``agents/{key}:retire`` of X holds it and may wait for that task."""
    s = await process_setup(client)
    created = await client.post(
        "/api/v1/agents",
        json={
            "key": "worker-x",
            "spec": {
                "displayName": "Worker X",
                "identity": {"kind": "agent", "permissions": ["tasks.read"]},
                "placement": "none",
            },
        },
        headers=auth(s["key"]),
    )
    assert created.status_code in (200, 201), created.text
    _, fleet_key = await create_agent_with_key(
        client,
        s["key"],
        name="fleet-x",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    linked = await client.put(
        "/api/v1/agents/worker-x/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(fleet_key),
    )
    assert linked.status_code == 200, linked.text
    worker_x = linked.json()["principalId"]

    await publish_process(
        client, s["key"], "first", _one_step_process("sample.first", [{"principal": s["admin"]}])
    )
    calling = _one_step_process("sample.second", [])
    calling["stages"][0]["steps"][0] = {"id": "review", "call": {"agent": "worker-x"}}
    await publish_process(client, s["key"], "second", calling)
    for kind in ("sample.first", "sample.second"):
        observed = await client.post(
            "/api/v1/observations",
            json={"kind": kind, "content": "seen", "data": {"n": "1"}},
            headers=auth(s["key"]),
        )
        assert observed.status_code in (200, 201), observed.text

    worker = Worker(settings)
    try:
        with _row_locked_for_update(sync_engine, "principals", worker_x) as holder:
            batch = asyncio.create_task(worker.process_events())
            await _wait_until_blocked_by(sync_engine, holder)
            _assert_touched_no_task_row(
                sync_engine,
                _blocked_backend(sync_engine, holder),
                "the process filed work before taking the agent its call step names",
            )
        await batch
    finally:
        await worker.engine.dispose()

    with sync_engine.connect() as conn:
        assignees = (
            conn.execute(text("SELECT assignee_id FROM tasks ORDER BY created_at")).scalars().all()
        )
    assert [str(a) for a in assignees] == [s["admin"], worker_x]
