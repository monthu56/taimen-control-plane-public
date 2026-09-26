"""Approve a code review, and the review closes by the result of the merge.

End to end on the M2.2 tree (CP-ADR-0061 §10): the approval outcome queues
``git.merge@1`` with the approval as its basis, the daemon's real
``SkillExecutor`` claims it — the claim re-checks that basis, so the review
must still be open — runs it and reports, and the outcome's reaction then
either closes the review (merged) or files work for the coder and leaves the
review open (not merged). Only the skill body is a stub.
"""

from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.main import Worker
from control_plane_agent.skills import LocalProtocol, SkillExecutor
from control_plane_client import ControlPlaneClient
from tests.helpers import auth, create_agent_with_key, do_bootstrap
from tests.integration.test_approval_outcome_skills import CODE_REVIEW_V4, MERGER_PERMISSIONS
from tests.integration.test_approval_outcomes import (
    _decide,
    _outcome,
    _review_setup,
    _task,
    _tasks_of_type,
)
from tests.skill_stubs import git_merge

Make = Callable[[str], ControlPlaneClient]


def _make_outcomes_due(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE approvals SET outcome_next_attempt_at = now() "
                "WHERE outcome_status = 'deferred'"
            )
        )


@pytest.mark.parametrize("branch", ["task/TASK-000001", "task/TASK-000001-conflict"])
async def test_approved_review_closes_by_the_result_of_the_merge(
    client: httpx.AsyncClient,
    sdk: Make,
    settings: Settings,
    sync_engine: Engine,
    branch: str,
) -> None:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    published = await client.post(
        "/api/v1/skills",
        json={
            "name": "git.merge",
            "version": "1",
            "sideEffects": "external_write",
            "riskLevel": "medium",
            "contract": git_merge.CONTRACT,
        },
        headers=auth(admin),
    )
    assert published.status_code == 201, published.text
    s = await _review_setup(
        client,
        schema=CODE_REVIEW_V4,
        gate=False,
        reviewer_permissions=MERGER_PERMISSIONS,
        admin_key=admin,
    )
    review = await _task(client, admin, s["review"]["id"])
    fields = {
        "repository": "https://forge.example/org/control-plane.git",
        "branch": branch,
        "commit": "abc1234",
        "targetBranch": "main",
    }
    patched = await client.patch(
        f"/api/v1/tasks/{review['id']}",
        json={"customFields": fields},
        headers={**auth(admin), "If-Match": f'"task-{review["version"]}"'},
    )
    assert patched.status_code == 200, patched.text
    approval = await client.post(
        "/api/v1/approvals",
        json={"task": review["id"], "assignedPrincipalId": s["reviewer"]["id"], "gate": True},
        headers=auth(admin),
    )
    assert approval.status_code == 201, approval.text
    approval_id = approval.json()["id"]
    _, executor_key = await create_agent_with_key(
        client, admin, name="executor", permissions=["skills.execute"]
    )
    worker = Worker(settings)
    try:
        # approve -> skill_invocation queued, the review still open
        await _decide(client, s["reviewer_key"], approval_id, "approve")
        await worker.run_once()
        outcome = await _outcome(client, admin, approval_id)
        assert outcome["outcomeStatus"] == "deferred"
        invocation_id = outcome["actions"][0]["result"]["invocationId"]

        # claim -> run -> complete, by the daemon's executor
        async with sdk(executor_key) as runner:
            executor = SkillExecutor(
                runner,
                {"local": LocalProtocol([git_merge.ENTRYPOINT], isolation="thread")},
                local_entrypoints=[git_merge.ENTRYPOINT],
                poll_interval=0.05,
            )
            assert await executor.run_once(None) is True
        invocation = (
            await client.get(
                f"/api/v1/skill-invocations/{invocation_id}", headers=auth(s["reviewer_key"])
            )
        ).json()
        # Not cancelled as basis_revoked / task_terminal: the review was open.
        assert invocation["status"] == "succeeded", invocation
        assert invocation["inputs"]["branch"] == branch

        # the reaction to the result
        _make_outcomes_due(sync_engine)
        await worker.run_once()
    finally:
        await worker.engine.dispose()

    outcome = await _outcome(client, admin, approval_id)
    assert outcome["outcomeStatus"] == "executed"
    review = await _task(client, admin, review["id"])
    coding_tasks = _tasks_of_type(sync_engine, "coding-task")
    if branch.endswith("-conflict"):
        assert invocation["output"]["merged"] is False
        assert review["systemStatusCategory"] not in ("terminal_success", "terminal_failure")
        assert len(coding_tasks) == 2
        fix = coding_tasks[1]
        assert fix.title == f"Влить {s['coding']['publicId']} не удалось: conflict"
        assert str(fix.assignee_id) == s["coder"]["id"]
    else:
        assert invocation["output"]["merged"] is True
        assert review["systemStatusCategory"] == "terminal_success"
        assert len(coding_tasks) == 1
