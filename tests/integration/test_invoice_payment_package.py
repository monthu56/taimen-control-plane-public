"""M1.6 scenario 5: the verification stage on a second domain (CP-ADR-0067 §9, SC-007).

The ``invoice-payment`` fixture package (``tests/fixtures/packages/``) is
data only — a task type, a role, a skill and three rules — installed through
the public API the way any package is. A received invoice files work with
three checks of different kinds: an amount reconciliation by a deterministic
skill, the bank's "payment settled" fact, and the finance director's
decision. A withdrawn invoice cancels the work; a settled payment closes it
as done. Scenarios 1-4 of the spec run on it unchanged, next to a neutral
copy of another domain's rule pair in the same tenant, and neither package
touches the other's work.

Core has no line of code for this domain: if a test here needed one, the
stage would not be neutral.
"""

import importlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.catalog_packages import install_package
from tests.helpers import (
    assign_role,
    auth,
    claim_task,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
    open_session,
)

PACKAGE = "invoice-payment"
RECEIVED = "invoice.received"
WITHDRAWN = "invoice.withdrawn"
SETTLED = "invoice.payment_settled"
ENTRYPOINT = "tests.skill_stubs.amount_match:run"
CHECKS = ["amount-matches", "payment-settled", "director-approved"]
EXECUTOR_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "skills.invoke",
]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    """A tenant with the package installed, its director, executors and a skill host."""
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    finance = await create_workspace(client, key, "finance")
    # The rule names the role by id: a variable of the installation, as in any package.
    roles = await install_package(client, key, PACKAGE, kinds={"Role"})
    role_id = roles["Role/finance-director"]["id"]
    await install_package(
        client,
        key,
        PACKAGE,
        variables={"INVOICE_WORKSPACE_ID": finance["id"], "FINANCE_DIRECTOR_ROLE_ID": role_id},
        kinds={"ArtifactType", "Skill", "TaskType", "WorkRule"},
    )
    director, director_key = await create_agent_with_key(
        client,
        key,
        name="director",
        permissions=["approvals.decide", "approvals.read", "tasks.read"],
        kind="human",
    )
    await assign_role(client, key, director["id"], role_id)
    runner, runner_key = await create_agent_with_key(
        client, key, name="runner", permissions=EXECUTOR_PERMISSIONS
    )
    treasurer, treasurer_key = await create_agent_with_key(
        client, key, name="treasurer", permissions=EXECUTOR_PERMISSIONS, kind="human"
    )
    _, host_key = await create_agent_with_key(
        client, key, name="skill-host", permissions=["skills.execute"]
    )
    return {
        "key": key,
        "workspace": finance["id"],
        "role": role_id,
        "director_key": director_key,
        "runner": runner["id"],
        "runner_key": runner_key,
        "treasurer": treasurer["id"],
        "treasurer_key": treasurer_key,
        "host_key": host_key,
    }


async def _observe(
    client: httpx.AsyncClient, key: str, kind: str, workspace: str | None = None, **data: Any
) -> str:
    body: dict[str, Any] = {"kind": kind, "content": f"{kind} seen", "data": data}
    if workspace is not None:
        body["workspaceId"] = workspace
    response = await client.post("/api/v1/observations", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    observation_id: str = response.json()["id"]
    return observation_id


async def _receive(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine, s: dict[str, Any], invoice: str
) -> str:
    """An invoice for 1250.00 arrives; the package files the work to pay it."""
    await _observe(
        client,
        s["key"],
        RECEIVED,
        s["workspace"],
        invoice=invoice,
        supplier="Northwind",
        amount="1250.00",
    )
    await worker.run_once()
    [task_id] = _tasks_of(sync_engine, PACKAGE)
    return task_id


def _tasks_of(sync_engine: Engine, type_key: str) -> list[str]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT t.id FROM tasks t JOIN task_types tt ON tt.id = t.type_id "
                "WHERE tt.key = :type AND t.origin->>'kind' = 'rule' ORDER BY t.created_at"
            ),
            {"type": type_key},
        ).all()
    return [str(row.id) for row in rows]


async def _task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()
    return task


async def _patch(client: httpx.AsyncClient, key: str, ref: str, **body: Any) -> dict[str, Any]:
    current = await _task(client, key, ref)
    response = await client.patch(
        f"/api/v1/tasks/{ref}",
        json=body,
        headers={**auth(key), "If-Match": f'"task-{current["version"]}"'},
    )
    assert response.status_code == 200, response.text
    patched: dict[str, Any] = response.json()
    return patched


async def _verifications(client: httpx.AsyncClient, key: str, ref: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/tasks/{ref}/verifications", headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _start(
    client: httpx.AsyncClient, runner_key: str, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The agent executor takes the work and starts a run on it."""
    session = await open_session(client, runner_key)
    claimed = await claim_task(client, runner_key, task_id, session["id"])
    assert claimed.status_code == 200, claimed.text
    claim: dict[str, Any] = claimed.json()
    started = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(runner_key),
    )
    assert started.status_code in (200, 201), started.text
    run: dict[str, Any] = started.json()
    return claim, run


async def _pay(client: httpx.AsyncClient, runner_key: str, task_id: str, amount: str) -> str:
    """The agent pays, records the payment order's amount and reports the run done."""
    claim, run = await _start(client, runner_key, task_id)
    fields = (await _task(client, runner_key, task_id))["customFields"]
    await _patch(
        client,
        runner_key,
        task_id,
        # A whole-document replace: the invoice's fields the rule wrote stay.
        customFields={**fields, "paymentAmount": amount},
        claimId=claim["id"],
        fencingToken=claim["fencingToken"],
    )
    succeeded = await client.post(
        f"/api/v1/runs/{run['id']}:succeed", json={}, headers=auth(runner_key)
    )
    assert succeeded.status_code == 200, succeeded.text
    run_id: str = run["id"]
    return run_id


async def _host_skill_call(client: httpx.AsyncClient, host_key: str) -> dict[str, Any]:
    """The skill host takes the queued call and runs the package's skill on its inputs."""
    claimed = await client.post(
        "/api/v1/skill-invocations:claim",
        json={"protocols": ["local"], "localEntrypoints": [ENTRYPOINT]},
        headers=auth(host_key),
    )
    assert claimed.status_code == 200, claimed.text
    lease: dict[str, Any] = claimed.json()["invocation"]
    assert lease is not None
    module, function = ENTRYPOINT.split(":")
    output = getattr(importlib.import_module(module), function)(lease["inputs"])
    done = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": output},
        headers=auth(host_key),
    )
    assert done.status_code == 200, done.text
    return {**lease, "output": output}


def _make_due(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE task_verifications SET next_check_at = now() "
                "WHERE next_check_at IS NOT NULL"
            )
        )
        conn.execute(
            text("UPDATE rule_evaluations SET next_check_at = now() WHERE status = 'waiting'")
        )


async def _reconcile_amount(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine, host_key: str
) -> dict[str, Any]:
    """One pass queues the amount check, the host answers, the next pass reads it."""
    await worker.run_once()
    call = await _host_skill_call(client, host_key)
    _make_due(sync_engine)
    await worker.run_once()
    return call


async def _decide(
    client: httpx.AsyncClient, key: str, approval_id: str, verb: str, comment: str | None = None
) -> None:
    response = await client.post(
        f"/api/v1/approvals/{approval_id}:{verb}",
        json={} if comment is None else {"comment": comment},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text


def _events(sync_engine: Engine, event_type: str, entity_id: str) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload FROM events WHERE event_type = :type AND entity_id = :entity "
                "ORDER BY sequence"
            ),
            {"type": event_type, "entity": entity_id},
        ).all()
    return [row.payload for row in rows]


def _approvals(sync_engine: Engine, task_id: str) -> list[Any]:
    with sync_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, status, required_role_id FROM approvals WHERE task_id = :task "
                    "ORDER BY created_at"
                ),
                {"task": task_id},
            ).all()
        )


async def _rule_evaluations(
    client: httpx.AsyncClient, key: str, rule_key: str
) -> list[dict[str, Any]]:
    rules = (await client.get("/api/v1/rules", headers=auth(key))).json()["items"]
    [rule] = [r for r in rules if r["key"] == rule_key]
    response = await client.get(
        f"/api/v1/rules/{rule['id']}/evaluations", params={"limit": 100}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- scenario 5.1: the checks in order, done after the bank and the director ----------


async def test_a_paid_invoice_is_done_only_after_the_bank_and_the_director(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Scenarios 1, 2 and 4 on the package: amount, the bank's fact, the decision."""
    s = await _setup(client)
    key = s["key"]
    task_id = await _receive(client, worker, sync_engine, s, "INV-7")
    task = await _task(client, key, task_id)
    assert (task["typeKey"], task["status"], task["workspaceId"]) == (
        PACKAGE,
        "to_pay",
        s["workspace"],
    )
    # The rule gave the work its acceptance and its fields, rendered from the invoice.
    assert [c["key"] for c in task["acceptance"]] == CHECKS
    assert task["customFields"] == {
        "invoice": "INV-7",
        "supplier": "Northwind",
        "invoiceAmount": "1250.00",
    }
    assert task["acceptance"][0]["spec"]["inputs"] == {
        "invoiceAmount": "1250.00",
        "paymentAmount": "$.task.customFields.paymentAmount",
    }

    # The agent pays and reports; the run hands the work in, it does not finish it.
    run_id = await _pay(client, s["runner_key"], task_id, "1250.00")
    handed_in = await _task(client, key, task_id)
    assert handed_in["systemStatusCategory"] != "terminal_success"
    assert handed_in["activeClaimId"] is None

    call = await _reconcile_amount(client, worker, sync_engine, s["host_key"])
    assert call["inputs"] == {"invoiceAmount": "1250.00", "paymentAmount": "1250.00"}
    assert call["output"] == {"matches": True, "difference": "0.00"}
    [attempt] = await _verifications(client, key, task_id)
    assert (attempt["trigger"], attempt["triggerRef"], attempt["authorityPrincipalId"]) == (
        "run",
        run_id,
        s["runner"],
    )
    # The amount holds; the bank has not paid yet, so the director is not asked yet.
    assert attempt["status"] == "waiting_external"
    assert [(r["key"], r["status"]) for r in attempt["results"]] == [("amount-matches", "passed")]
    assert _approvals(sync_engine, task_id) == []

    # The bank's fact: the closing rule ties it to its check and wakes the attempt.
    settled = await _observe(client, key, SETTLED, s["workspace"], invoice="INV-7")
    await worker.run_once()
    [attempt] = await _verifications(client, key, task_id)
    assert attempt["status"] == "waiting_human"
    [approval] = _approvals(sync_engine, task_id)
    assert (str(approval.id), approval.status, str(approval.required_role_id)) == (
        attempt["approvalId"],
        "pending",
        s["role"],
    )
    current = await _task(client, key, task_id)
    assert current["systemStatusCategory"] != "terminal_success"
    assert {"kind": "observation", "observationId": settled, "check": "payment-settled"} in current[
        "evidence"
    ]

    decided = attempt["approvalId"]
    await _decide(client, s["director_key"], decided, "approve")
    await worker.run_once()
    done = await _task(client, key, task_id)
    assert (done["status"], done["systemStatusCategory"]) == ("paid", "terminal_success")
    [attempt] = await _verifications(client, key, task_id)
    assert attempt["status"] == "passed"
    amount, bank, director = attempt["results"]
    assert [(r["key"], r["kind"], r["status"]) for r in attempt["results"]] == [
        ("amount-matches", "deterministic", "passed"),
        ("payment-settled", "external_state", "passed"),
        ("director-approved", "human", "passed"),
    ]
    # Evidence of each: the skill call, the bank's fact, the director's decision.
    assert {"kind": "skill_invocation", "ref": call["id"]} in amount["evidence"]
    assert bank["evidence"] == [{"kind": "observation", "observationId": settled}]
    assert director["evidence"] == [{"kind": "approval", "ref": decided}]
    assert len(_events(sync_engine, "task.completed", task_id)) == 1
    [verified] = _events(sync_engine, "task.verified", task_id)
    assert [r["key"] for r in verified["results"]] == CHECKS

    # The same settlement again changes nothing (FR-011).
    await _observe(client, key, SETTLED, s["workspace"], invoice="INV-7")
    await worker.run_once()
    latest = (await _rule_evaluations(client, key, "payment-settled"))[0]
    assert latest["result"]["work"][0]["reason"] == "no_open_work"
    assert len(await _verifications(client, key, task_id)) == 1
    assert len(_events(sync_engine, "task.verified", task_id)) == 1
    assert (await _task(client, key, task_id))["version"] == done["version"]


# --- the invoice's fields: the package's type decides what fits (CP-ADR-0063, B1) -


async def test_an_invoice_the_type_does_not_accept_files_no_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The rule's customFields meet the package's fieldSchema, not a rule of core."""
    s = await _setup(client)
    key = s["key"]
    await _observe(
        client, key, RECEIVED, s["workspace"], invoice="INV-9", supplier="Northwind", amount="12,5"
    )
    await worker.run_once()
    assert _tasks_of(sync_engine, PACKAGE) == []
    [evaluation] = await _rule_evaluations(client, key, "invoice-received")
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "custom_fields_invalid"

    # The same invoice with an amount the type accepts: the work carries its fields.
    task_id = await _receive(client, worker, sync_engine, s, "INV-9")
    assert (await _task(client, key, task_id))["customFields"]["invoiceAmount"] == "1250.00"


# --- scenarios 1 and 4: a failed check returns the work to whoever does it ----------


async def test_a_wrong_amount_and_a_rejection_return_the_work_to_its_executor(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    task_id = await _receive(client, worker, sync_engine, s, "INV-8")

    await _pay(client, s["runner_key"], task_id, "1205.00")
    call = await _reconcile_amount(client, worker, sync_engine, s["host_key"])
    assert call["output"] == {"matches": False, "difference": "-45.00"}
    [attempt] = await _verifications(client, key, task_id)
    assert attempt["status"] == "failed"
    assert [(r["key"], r["reason"]) for r in attempt["results"]] == [
        ("amount-matches", "expectation_not_met")
    ]
    # Back to be done again, without a person; the bank and the director never came up.
    task = await _task(client, key, task_id)
    assert task["status"] == "to_pay"
    claimability = (
        await client.get(f"/api/v1/tasks/{task_id}/claimability", headers=auth(key))
    ).json()
    assert claimability["claimable"] is True, claimability
    assert _approvals(sync_engine, task_id) == []
    comments = (await client.get(f"/api/v1/tasks/{task_id}/comments", headers=auth(key))).json()
    assert "amount-matches" in comments["items"][0]["body"]

    # A person corrects the payment and hands it in: the stage does not tell them apart.
    fields = (await _task(client, key, task_id))["customFields"]
    await _patch(
        client, s["treasurer_key"], task_id, customFields={**fields, "paymentAmount": "1250.00"}
    )
    current = await _task(client, key, task_id)
    handed_in = await client.post(
        f"/api/v1/tasks/{task_id}:complete",
        headers={**auth(s["treasurer_key"]), "If-Match": f'"task-{current["version"]}"'},
    )
    assert handed_in.status_code == 200, handed_in.text
    await _reconcile_amount(client, worker, sync_engine, s["host_key"])
    await _observe(client, key, SETTLED, s["workspace"], invoice="INV-8")
    await worker.run_once()
    attempt = (await _verifications(client, key, task_id))[0]
    assert (attempt["attempt"], attempt["trigger"], attempt["status"]) == (
        2,
        "complete",
        "waiting_human",
    )
    assert attempt["authorityPrincipalId"] == s["treasurer"]

    await _decide(
        client, s["director_key"], attempt["approvalId"], "reject", "Paid from the wrong account"
    )
    await worker.run_once()
    attempt = (await _verifications(client, key, task_id))[0]
    assert attempt["status"] == "failed"
    assert [(r["key"], r["status"]) for r in attempt["results"]] == [
        ("amount-matches", "passed"),
        ("payment-settled", "passed"),
        ("director-approved", "failed"),
    ]
    assert attempt["results"][2]["reason"] == "approval_rejected"
    failures = _events(sync_engine, "task.verification_failed", task_id)
    assert [(f["failedCheck"], f["consecutiveFailures"], f["blocked"]) for f in failures] == [
        ("amount-matches", 1, False),
        ("director-approved", 2, False),
    ]
    assert (await _task(client, key, task_id))["status"] == "to_pay"
    comments = (await client.get(f"/api/v1/tasks/{task_id}/comments", headers=auth(key))).json()
    assert any("Paid from the wrong account" in c["body"] for c in comments["items"])
    assert _events(sync_engine, "task.completed", task_id) == []


# --- scenarios 5.2 and 3: a withdrawn invoice cancels the work ------------------------


async def test_a_withdrawn_invoice_cancels_the_work_and_its_open_checks(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    task_id = await _receive(client, worker, sync_engine, s, "INV-9")
    await _pay(client, s["runner_key"], task_id, "1250.00")
    await _reconcile_amount(client, worker, sync_engine, s["host_key"])
    assert (await _verifications(client, key, task_id))[0]["status"] == "waiting_external"

    withdrawn = await _observe(client, key, WITHDRAWN, s["workspace"], invoice="INV-9")
    await worker.run_once()
    task = await _task(client, key, task_id)
    assert (task["status"], task["systemStatusCategory"]) == ("withdrawn", "terminal_cancelled")
    assert {"kind": "observation", "observationId": withdrawn} in task["evidence"]
    assert [a["status"] for a in await _verifications(client, key, task_id)] == ["cancelled"]

    # A settlement that comes late finds no work to close.
    await _observe(client, key, SETTLED, s["workspace"], invoice="INV-9")
    await worker.run_once()
    assert (await _task(client, key, task_id))["version"] == task["version"]
    assert _approvals(sync_engine, task_id) == []
    assert _events(sync_engine, "task.verified", task_id) == []


async def test_a_withdrawn_invoice_stops_the_executor_before_cancelling(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    task_id = await _receive(client, worker, sync_engine, s, "INV-10")
    claim, run = await _start(client, s["runner_key"], task_id)

    withdrawn = await _observe(client, key, WITHDRAWN, s["workspace"], invoice="INV-10")
    await worker.run_once()
    current = (await client.get(f"/api/v1/runs/{run['id']}", headers=auth(key))).json()
    assert current["cancelRequestedAt"] is not None
    assert (await _task(client, key, task_id))["status"] == "paying"
    [evaluation] = await _rule_evaluations(client, key, "invoice-withdrawn")
    assert evaluation["status"] == "waiting"

    stopped = await client.post(
        f"/api/v1/runs/{run['id']}:cancel", json={"reason": "asked"}, headers=auth(s["runner_key"])
    )
    assert stopped.status_code == 200, stopped.text
    released = await client.post(
        f"/api/v1/claims/{claim['id']}:release",
        json={"reason": "cancelled"},
        headers=auth(s["runner_key"]),
    )
    assert released.status_code == 200, released.text
    _make_due(sync_engine)
    await worker.run_once()
    task = await _task(client, key, task_id)
    assert task["status"] == "withdrawn"
    assert task["evidence"] == [{"kind": "observation", "observationId": withdrawn}]
    assert len(_events(sync_engine, "work.reconciled", task_id)) == 1


# --- scenario 5.3: two packages in one tenant ---------------------------------------

# A neutral copy of another domain's pair (the self-development package's
# ci-red / ci-green): a red run files work, the same job green again closes it.
RUN_OBSERVED = "sample.run_observed"
OTHER_DOMAIN_RULES: list[dict[str, Any]] = [
    {
        "key": "sample-red",
        "trigger": {"kind": "observation", "type": RUN_OBSERVED},
        "condition": {"eq": [{"var": "payload.data.conclusion"}, "failure"]},
        "action": {
            "kind": "ensure_work",
            "taskType": "task",
            "dedupKeyTemplate": "sample-red:{{payload.data.job}}",
            "fields": {"title": "Sample job {{payload.data.job}} is red"},
            "acceptance": [
                {
                    "key": "green-again",
                    "kind": "external_state",
                    "description": "The job is green again",
                    "spec": {"event": RUN_OBSERVED},
                }
            ],
        },
    },
    {
        "key": "sample-green",
        "trigger": {"kind": "observation", "type": RUN_OBSERVED},
        "condition": {"eq": [{"var": "payload.data.conclusion"}, "success"]},
        "action": {
            "kind": "complete_work",
            "dedupKeyTemplate": "sample-red:{{payload.data.job}}",
            "check": "green-again",
        },
    },
]


async def test_two_packages_in_one_tenant_do_not_touch_each_others_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    for rule in OTHER_DOMAIN_RULES:
        response = await client.post("/api/v1/rules", json=rule, headers=auth(key))
        assert response.status_code == 201, response.text

    # The same identifier in both domains, facts interleaved.
    await _observe(client, key, RUN_OBSERVED, job="7", conclusion="failure")
    invoice_task = await _receive(client, worker, sync_engine, s, "7")
    [other_task] = _tasks_of(sync_engine, "task")

    green = await _observe(client, key, RUN_OBSERVED, job="7", conclusion="success")
    await worker.run_once()
    other = await _task(client, key, other_task)
    assert (other["status"], other["systemStatusCategory"]) == ("done", "terminal_success")
    assert other["evidence"] == [
        {"kind": "observation", "observationId": green, "check": "green-again"}
    ]
    # The other domain's closing fact did not reach the invoice.
    invoice = await _task(client, key, invoice_task)
    assert (invoice["status"], invoice["evidence"]) == ("to_pay", [])
    assert await _verifications(client, key, invoice_task) == []

    withdrawn = await _observe(client, key, WITHDRAWN, s["workspace"], invoice="7")
    await worker.run_once()
    invoice = await _task(client, key, invoice_task)
    assert invoice["status"] == "withdrawn"
    assert invoice["evidence"] == [{"kind": "observation", "observationId": withdrawn}]
    assert (await _task(client, key, other_task))["version"] == other["version"]

    # Each package's rules looked at their own facts only.
    for rule_key, evaluated in (
        ("invoice-received", 1),
        ("invoice-withdrawn", 1),
        ("payment-settled", 0),
    ):
        assert len(await _rule_evaluations(client, key, rule_key)) == evaluated, rule_key
    [other_attempt] = await _verifications(client, key, other_task)
    assert [(r["key"], r["status"]) for r in other_attempt["results"]] == [
        ("green-again", "passed")
    ]
