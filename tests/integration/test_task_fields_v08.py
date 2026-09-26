"""Work item custom fields, planned dates and the filters over them (ADR-0049).

Three guarantees are under test here.

*Fields are a contract, not a bag.* A task's ``custom_fields`` are checked
against the ``field_schema`` of the type version the task pins, so a tenant's
own vocabulary is enforced by the same machinery that enforces a project
profile's — and the secret scan runs on them, because a task is edited by
agents far more often than a project profile is.

*Dates are columns.* They are filtered and ordered in SQL, and an interval that
runs backwards is refused by the application and by the database.

*Paging stays honest.* Filters are part of the statement before the cursor
predicate, and the date ordering is total — equal due dates are still separated
by id — so no page can skip or repeat a row.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.helpers import auth, create_task, do_bootstrap, make_tenant_directly

INCIDENT_FIELDS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "component": {"type": "string"},
        "blastRadius": {"type": "string", "enum": ["one_service", "platform"]},
    },
    "required": ["component"],
    "additionalProperties": False,
}

DAY = timedelta(days=1)
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def iso(moment: datetime) -> str:
    """Wire form of an instant; the response comes back as ...Z, so compare
    parsed instants rather than strings."""
    return moment.isoformat()


def at(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


async def create_incident_type(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "incident",
            "displayName": "Incident",
            "fieldSchema": INCIDENT_FIELDS,
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def patch_task(
    client: httpx.AsyncClient, key: str, task: dict[str, Any], **fields: Any
) -> httpx.Response:
    return await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json=fields,
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )


# --- custom fields against the type's schema ----------------------------------


async def test_custom_fields_are_validated_against_the_type_that_the_task_pins(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_incident_type(client, admin_key)

    task = await create_task(
        client,
        admin_key,
        title="Prod outage",
        typeKey="incident",
        customFields={"component": "iam", "blastRadius": "platform"},
    )
    assert task["customFields"] == {"component": "iam", "blastRadius": "platform"}

    response = await client.post(
        "/api/v1/tasks",
        json={"title": "Another outage", "typeKey": "incident", "customFields": {"who": "knows"}},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "custom_fields_invalid"


async def test_the_system_type_still_accepts_any_fields(client: httpx.AsyncClient) -> None:
    # Its field_schema is {} — every task that existed before WI-3 carries it.
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    task = await create_task(client, admin_key, customFields={"anything": [1, 2, 3]})

    assert task["customFields"] == {"anything": [1, 2, 3]}


async def test_secrets_are_refused_in_custom_fields(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await client.post(
        "/api/v1/tasks",
        json={"title": "Deploy", "customFields": {"deploy": {"apiKey": "sk-live-1234"}}},
        headers=auth(admin_key),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "secret_material_rejected"


async def test_custom_fields_are_replaced_wholesale_and_never_merged(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, customFields={"a": 1, "b": 2})

    response = await patch_task(client, admin_key, task, customFields={"a": 9})

    assert response.status_code == 200
    assert response.json()["customFields"] == {"a": 9}
    assert response.json()["version"] == 2


async def test_null_custom_fields_is_refused_because_clearing_is_an_empty_object(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, customFields={"a": 1})

    response = await patch_task(client, admin_key, task, customFields=None)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_field"

    cleared = await patch_task(client, admin_key, task, customFields={})
    assert cleared.status_code == 200
    assert cleared.json()["customFields"] == {}


async def test_updating_fields_is_checked_against_the_pinned_type_version(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_incident_type(client, admin_key)
    task = await create_task(
        client, admin_key, typeKey="incident", customFields={"component": "iam"}
    )

    response = await patch_task(client, admin_key, task, customFields={"component": 42})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "custom_fields_invalid"


async def test_the_journal_records_that_fields_changed_but_not_what_they_are(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key)
    assert (
        await patch_task(client, admin_key, task, customFields={"customerEmail": "a@b.test"})
    ).status_code == 200

    events = (await client.get("/api/v1/events?tail=50", headers=auth(admin_key))).json()["items"]
    updated = [e for e in events if e["type"] == "task.updated"]
    assert updated, events
    assert updated[-1]["payload"]["changes"]["custom_fields"] is True
    assert "a@b.test" not in str(events)


# --- planned dates ------------------------------------------------------------


async def test_planned_dates_round_trip_and_may_be_cleared(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    task = await create_task(client, admin_key, startDate=iso(T0), dueDate=iso(T0 + 7 * DAY))
    assert at(task["startDate"]) == T0
    assert at(task["dueDate"]) == T0 + 7 * DAY

    response = await patch_task(client, admin_key, task, dueDate=None)
    assert response.status_code == 200
    assert response.json()["dueDate"] is None
    assert at(response.json()["startDate"]) == T0


async def test_a_task_without_dates_is_the_default(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    task = await create_task(client, admin_key)

    assert task["startDate"] is None
    assert task["dueDate"] is None
    assert task["customFields"] == {}


async def test_a_backwards_interval_is_refused_on_create(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await client.post(
        "/api/v1/tasks",
        json={"title": "Time travel", "startDate": iso(T0 + DAY), "dueDate": iso(T0)},
        headers=auth(admin_key),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_planned_dates"


async def test_moving_one_end_is_checked_against_the_stored_other_end(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, dueDate=iso(T0))

    refused = await patch_task(client, admin_key, task, startDate=iso(T0 + DAY))
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_planned_dates"
    # A refused update leaves the version alone, so the caller's If-Match holds.
    assert (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()[
        "version"
    ] == 1

    allowed = await patch_task(client, admin_key, task, startDate=iso(T0 - DAY))
    assert allowed.status_code == 200


async def test_the_database_refuses_a_backwards_interval_too(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key)

    with pytest.raises(DBAPIError) as exc, sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET start_date = :late, due_date = :early WHERE id = :id"),
            {"late": T0 + DAY, "early": T0, "id": task["id"]},
        )

    assert "ck_tasks_planned_dates_ordered" in str(exc.value)


# --- filters and ordering -----------------------------------------------------


async def _dated_tasks(client: httpx.AsyncClient, key: str) -> dict[str, dict[str, Any]]:
    """Three tasks due on the SAME day, one earlier, one with no due date."""
    made = {}
    for title, due in (
        ("early", T0),
        ("same-a", T0 + 7 * DAY),
        ("same-b", T0 + 7 * DAY),
        ("same-c", T0 + 7 * DAY),
        ("undated", None),
    ):
        extra = {"dueDate": iso(due)} if due is not None else {}
        made[title] = await create_task(client, key, title=title, **extra)
    return made


async def list_tasks(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/tasks", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def test_owner_filter(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key, owner = body["apiKey"]["key"], body["adminPrincipal"]["id"]
    mine = await create_task(client, admin_key, title="mine", ownerId=owner)
    await create_task(client, admin_key, title="unowned")

    page = await list_tasks(client, admin_key, ownerId=owner)

    assert [t["id"] for t in page["items"]] == [mine["id"]]


async def test_date_bounds_are_inclusive_and_skip_undated_tasks(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    made = await _dated_tasks(client, admin_key)

    page = await list_tasks(client, admin_key, dueFrom=iso(T0), dueTo=iso(T0))
    assert [t["title"] for t in page["items"]] == ["early"]

    later = await list_tasks(client, admin_key, dueFrom=iso(T0 + DAY))
    assert sorted(t["title"] for t in later["items"]) == ["same-a", "same-b", "same-c"]
    assert made["undated"]["id"] not in {t["id"] for t in later["items"]}


async def test_start_date_bounds_are_independent_of_due_date(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_task(client, admin_key, title="starts-soon", startDate=iso(T0))
    await create_task(client, admin_key, title="starts-later", startDate=iso(T0 + 30 * DAY))

    page = await list_tasks(client, admin_key, startTo=iso(T0 + DAY))

    assert [t["title"] for t in page["items"]] == ["starts-soon"]


async def test_due_date_ordering_is_soonest_first_with_undated_tasks_last(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _dated_tasks(client, admin_key)

    page = await list_tasks(client, admin_key, sort="dueDate")

    titles = [t["title"] for t in page["items"]]
    assert titles[0] == "early"
    assert set(titles[1:4]) == {"same-a", "same-b", "same-c"}
    assert titles[4] == "undated"


async def test_date_paging_covers_every_row_exactly_once(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _dated_tasks(client, admin_key)

    seen: list[str] = []
    params: dict[str, Any] = {"sort": "dueDate", "limit": 2}
    for _ in range(5):
        page = await list_tasks(client, admin_key, **params)
        seen.extend(t["id"] for t in page["items"])
        if page["nextCursor"] is None:
            break
        params = {"sort": "dueDate", "limit": 2, "cursor": page["nextCursor"]}

    assert page["nextCursor"] is None
    assert len(seen) == len(set(seen)) == 5


async def test_a_filter_narrows_the_page_before_the_cursor_is_applied(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _dated_tasks(client, admin_key)

    first = await list_tasks(client, admin_key, sort="dueDate", limit=2, dueFrom=iso(T0 + DAY))
    assert len(first["items"]) == 2
    second = await list_tasks(
        client,
        admin_key,
        sort="dueDate",
        limit=2,
        dueFrom=iso(T0 + DAY),
        cursor=first["nextCursor"],
    )

    titles = [t["title"] for t in first["items"] + second["items"]]
    assert sorted(titles) == ["same-a", "same-b", "same-c"]
    assert second["nextCursor"] is None


async def test_a_cursor_from_another_ordering_is_refused(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _dated_tasks(client, admin_key)
    created_page = await list_tasks(client, admin_key, limit=2)

    response = await client.get(
        "/api/v1/tasks",
        params={"sort": "dueDate", "limit": 2, "cursor": created_page["nextCursor"]},
        headers=auth(admin_key),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


async def test_an_unknown_sort_is_refused(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await client.get(
        "/api/v1/tasks", params={"sort": "whenever"}, headers=auth(admin_key)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_sort"


async def test_dates_and_fields_do_not_cross_the_tenant_boundary(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_task(client, admin_key, title="ours", dueDate=iso(T0), customFields={"a": 1})
    _, other_key = make_tenant_directly(sync_engine, "other")

    theirs = await list_tasks(client, other_key, sort="dueDate", dueFrom=iso(T0 - DAY))

    assert theirs["items"] == []
