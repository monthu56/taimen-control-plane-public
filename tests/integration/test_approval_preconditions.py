"""Preconditions of an approve (TAI-ADR-0041 p.7, CP-ADR-0061 amendment).

The type of a review says "approve only once the branch's CI is green", and
the CI verdict reaches core as an external observation (CP-ADR-0057). While
it is missing or red, ``approve`` answers ``409 approval_precondition_failed``
with the reason, and nothing is decided: the gate stays pending, no event is
written, no outcome is set in motion. ``reject`` is never held back.
"""

import copy
from typing import Any

import httpx

from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap
from tests.integration.test_approval_outcomes import (
    CODE_REVIEW_SCHEMA,
    REVIEWER_PERMISSIONS,
    _create_type,
    _decide,
    _events,
    _gate,
    _review_setup,
)

CI_GREEN: dict[str, Any] = {
    "observation": {"kind": "ci.status", "source": "ci", "task": "$.spawnedBy.id!"},
    "condition": {"eq": [{"var": "observation.data.conclusion"}, "success"]},
    "reason": "CI of $.spawnedBy.publicId is not green",
}


def _with_preconditions(*preconditions: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(CODE_REVIEW_SCHEMA)
    schema["gates"]["default"]["preconditions"] = {"approved": list(preconditions)}
    return schema


async def _observe(
    client: httpx.AsyncClient,
    key: str,
    *,
    conclusion: str,
    observed_at: str,
    task: str | None = None,
    source: str = "ci",
    external_id: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "kind": "ci.status",
        "content": f"CI finished: {conclusion}",
        "data": {"conclusion": conclusion},
        "source": source,
        "observedAt": observed_at,
    }
    if task is not None:
        body["task"] = task
    if external_id is not None:
        body["externalRef"] = {"system": source, "id": external_id}
    response = await client.post("/api/v1/observations", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    observation_id: str = response.json()["id"]
    return observation_id


async def _approve(client: httpx.AsyncClient, key: str, approval_id: str) -> httpx.Response:
    return await client.post(f"/api/v1/approvals/{approval_id}:approve", json={}, headers=auth(key))


async def _refused(client: httpx.AsyncClient, key: str, approval_id: str) -> dict[str, Any]:
    response = await _approve(client, key, approval_id)
    assert response.status_code == 409, response.text
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == "approval_precondition_failed"
    return error


async def test_approve_waits_for_the_green_ci_of_the_source_task(
    client: httpx.AsyncClient,
) -> None:
    s = await _review_setup(client, schema=_with_preconditions(CI_GREEN))
    approval_id = s["approval"]["id"]
    coding, admin_key = s["coding"], s["admin_key"]
    reason = f"CI of {coding['publicId']} is not green"

    # No verdict yet.
    error = await _refused(client, s["reviewer_key"], approval_id)
    assert reason in error["message"]
    assert error["details"]["failed"] == [
        {"index": 0, "kind": "ci.status", "cause": "no_observation", "reason": reason}
    ]

    # Red.
    red = await _observe(
        client,
        admin_key,
        conclusion="failure",
        observed_at="2026-09-25T10:00:00+00:00",
        task=coding["id"],
    )
    error = await _refused(client, s["reviewer_key"], approval_id)
    assert error["details"]["failed"][0]["cause"] == "condition_false"
    assert error["details"]["failed"][0]["observationId"] == red

    # Green from another system does not count, nor does it for another task.
    await _observe(
        client,
        admin_key,
        conclusion="success",
        observed_at="2026-09-25T11:00:00+00:00",
        task=coding["id"],
        source="other-ci",
    )
    await _observe(
        client,
        admin_key,
        conclusion="success",
        observed_at="2026-09-25T11:00:00+00:00",
        task=s["review"]["id"],
    )
    await _refused(client, s["reviewer_key"], approval_id)

    # Nothing was decided on the way.
    pending = await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(admin_key))
    assert pending.json()["status"] == "pending"
    assert await _events(client, admin_key, "approval.approved") == []

    # Green — and a late-arriving older red does not undo it: the newest by
    # observedAt is what the precondition reads.
    await _observe(
        client,
        admin_key,
        conclusion="success",
        observed_at="2026-09-25T12:00:00+00:00",
        task=coding["id"],
    )
    await _observe(
        client,
        admin_key,
        conclusion="failure",
        observed_at="2026-09-25T09:00:00+00:00",
        task=coding["id"],
    )
    approved = await _approve(client, s["reviewer_key"], approval_id)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["outcomeStatus"] == "pending"


async def test_reject_is_never_held_back_by_a_precondition(client: httpx.AsyncClient) -> None:
    s = await _review_setup(client, schema=_with_preconditions(CI_GREEN))
    decided = await _decide(client, s["reviewer_key"], s["approval"]["id"], "reject", "Needs tests")
    assert decided["status"] == "rejected"


async def test_precondition_about_an_external_object_named_by_the_source_branch(
    client: httpx.AsyncClient,
) -> None:
    """``externalRef`` picks the observation of the branch itself, not of a task."""
    by_branch = {
        "observation": {
            "kind": "ci.status",
            "externalRef": "$.spawnedBy.artifact[commit].metadata.branch!",
        },
        "condition": {"eq": [{"var": "observation.data.conclusion"}, "success"]},
        "reason": "CI of $.spawnedBy.artifact[commit].metadata.branch is not green",
    }
    s = await _review_setup(client, schema=_with_preconditions(by_branch))
    branch = f"task/{s['coding']['publicId']}"

    await _observe(
        client,
        s["admin_key"],
        conclusion="success",
        observed_at="2026-09-25T12:00:00+00:00",
        external_id="task/SOMETHING-ELSE",
    )
    error = await _refused(client, s["reviewer_key"], s["approval"]["id"])
    assert f"CI of {branch} is not green" in error["message"]

    await _observe(
        client,
        s["admin_key"],
        conclusion="success",
        observed_at="2026-09-25T12:00:00+00:00",
        external_id=branch,
    )
    assert (await _approve(client, s["reviewer_key"], s["approval"]["id"])).status_code == 200


async def test_an_unresolved_reference_holds_the_approve_back(
    client: httpx.AsyncClient,
) -> None:
    """A review spawned by nothing has no source CI to vouch for it."""
    s = await _review_setup(client, schema=_with_preconditions(CI_GREEN), spawned=False)
    error = await _refused(client, s["reviewer_key"], s["approval"]["id"])
    assert error["details"]["failed"][0]["cause"] == "unresolved_reference"


async def test_without_task_or_external_ref_the_gated_task_is_observed(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    schema = {
        "gates": {
            "default": {
                "preconditions": {
                    "approved": [
                        {"observation": {"kind": "legal.signoff"}, "reason": "No legal sign-off"}
                    ]
                }
            }
        }
    }
    created = await _create_type(client, admin_key, "release", approvalSchema=schema)
    assert created.status_code == 201, created.text
    approver, approver_key = await create_agent_with_key(
        client, admin_key, name="approver", permissions=REVIEWER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Release 1.0", typeKey="release")
    approval = await _gate(client, admin_key, task["id"], approver["id"])

    error = await _refused(client, approver_key, approval["id"])
    assert error["message"].endswith("No legal sign-off")

    response = await client.post(
        "/api/v1/observations",
        json={"kind": "legal.signoff", "content": "Signed off", "task": task["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    approved = await _approve(client, approver_key, approval["id"])
    assert approved.status_code == 200, approved.text
    # Preconditions alone set no outcome in motion.
    assert approved.json()["outcomeStatus"] is None
