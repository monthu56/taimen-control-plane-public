"""The process-packages contract that outlives its steps (CP-ADR-0074, P002).

Every route of the contract is implemented now: the calendars (P004), process
definitions (P006), ``excludedPrincipals`` (P008), process instances (P009),
package tests (P013), replay (P014), plan and apply (P015) are at work in
``test_calendars.py``, ``test_process_definitions.py``,
``test_approval_separation_of_duties.py``, ``test_process_instances.py``,
``test_package_test.py``, ``test_process_replay.py`` and
``test_package_plan.py``. What stays here is what no step owns.
"""

import httpx

from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap


async def test_a_package_path_outside_the_package_is_refused(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    body = {"package": {"files": [{"path": "../secrets.yaml", "content": ""}]}}
    response = await client.post("/api/v1/packages:test", json=body, headers=auth(admin_key))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_an_approval_without_exclusions_works_as_before(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    decider, _ = await create_agent_with_key(client, admin_key, name="decider")
    task = await create_task(client, admin_key)
    body = {"task": task["id"], "assignedPrincipalId": decider["id"]}

    for extra in ({}, {"excludedPrincipals": []}):
        created = await client.post(
            "/api/v1/approvals", json={**body, **extra}, headers=auth(admin_key)
        )
        assert created.status_code == 201, created.text
        assert created.json()["excludedPrincipals"] == []
        read = await client.get(
            f"/api/v1/approvals/{created.json()['id']}", headers=auth(admin_key)
        )
        assert read.json()["excludedPrincipals"] == []
