"""Working-day calendars at work (CP-ADR-0074 §9, process-packages P004).

A version appears only when the canonical hash of the spec changes, and each
new one leaves a ``calendar.published`` event; versions are immutable and
addressed as ``key`` or ``key@version``; reading needs authentication alone,
publishing ``calendars.write``; a package object of kind ``Calendar`` installs
exactly like the route.
"""

import copy
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from control_plane.api.v1.calendars import install_calendar
from control_plane.application.authorization import AuthContext
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError
from tests.helpers import auth, create_agent_with_key, do_bootstrap

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
DOCUMENT: dict[str, Any] = yaml.safe_load(
    (FIXTURES / "ru-2024.calendar.yaml").read_text(encoding="utf-8")
)
BEFORE_CALENDARS = "a7c4e2d9b3f1"


def _spec(**changes: Any) -> dict[str, Any]:
    spec = copy.deepcopy(DOCUMENT["spec"])
    spec.update(changes)
    return spec


async def _publish(
    client: httpx.AsyncClient, key: str, spec: dict[str, Any], calendar: str = "ru"
) -> httpx.Response:
    return await client.post(
        "/api/v1/calendars", json={"key": calendar, "spec": spec}, headers=auth(key)
    )


async def _published(client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events", params={"types": "calendar.published"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_a_version_only_when_the_hash_changes(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    first = await _publish(client, admin_key, DOCUMENT["spec"])
    assert first.status_code == 201, first.text
    body = first.json()
    assert (body["key"], body["version"], body["latestVersion"]) == ("ru", 1, 1)
    assert body["calendarHash"].startswith("sha256:")
    assert body["provisionalYears"] == [2025]
    assert body["spec"]["years"][0]["workdays"] == ["2024-04-27", "2024-11-02", "2024-12-28"]

    again = await _publish(client, admin_key, DOCUMENT["spec"])
    assert again.status_code == 200, again.text
    assert again.json()["id"] == body["id"]

    # Reordering years and dates is not a change.
    years = [dict(year) for year in reversed(DOCUMENT["spec"]["years"])]
    years[1]["holidays"] = list(reversed(years[1]["holidays"]))
    reordered = await _publish(client, admin_key, _spec(years=years))
    assert reordered.status_code == 200, reordered.text
    assert reordered.json()["version"] == 1

    # 2025 confirmed: the provisional mark goes away with a new version.
    confirmed = _spec()
    confirmed["years"][1] = {**confirmed["years"][1], "provisional": False}
    second = await _publish(client, admin_key, confirmed)
    assert second.status_code == 201, second.text
    assert (second.json()["version"], second.json()["provisionalYears"]) == (2, [])
    assert second.json()["calendarHash"] != body["calendarHash"]

    events = await _published(client, admin_key)
    assert [event["payload"]["version"] for event in events] == [1, 2]
    latest = events[-1]
    assert latest["entityType"] == "calendar"
    assert latest["entityId"] == second.json()["id"]
    assert latest["payload"] == {
        "key": "ru",
        "version": 2,
        "calendarHash": second.json()["calendarHash"],
        "previousVersion": 1,
        "years": [2024, 2025],
        "provisionalYears": [],
    }


async def test_read_by_key_or_version_and_list_by_key(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["tasks.read"]
    )
    await _publish(client, admin_key, DOCUMENT["spec"])
    await _publish(client, admin_key, _spec(displayName="РФ, уточнён"))
    for calendar in ("by", "kz"):
        await _publish(client, admin_key, _spec(years=[{"year": 2024}]), calendar=calendar)

    latest = await client.get("/api/v1/calendars/ru", headers=auth(reader_key))
    assert latest.status_code == 200, latest.text
    assert (latest.json()["version"], latest.json()["latestVersion"]) == (2, 2)
    pinned = await client.get("/api/v1/calendars/ru@1", headers=auth(reader_key))
    assert pinned.status_code == 200, pinned.text
    assert (pinned.json()["version"], pinned.json()["latestVersion"]) == (1, 2)
    assert pinned.json()["spec"]["displayName"] == DOCUMENT["spec"]["displayName"]
    for missing in ("ru@3", "ru@x", "ru@", "de"):
        response = await client.get(f"/api/v1/calendars/{missing}", headers=auth(reader_key))
        assert response.status_code == 404, missing

    page = await client.get("/api/v1/calendars", params={"limit": 2}, headers=auth(reader_key))
    assert page.status_code == 200, page.text
    assert [item["key"] for item in page.json()["items"]] == ["by", "kz"]
    rest = await client.get(
        "/api/v1/calendars",
        params={"limit": 2, "cursor": page.json()["nextCursor"]},
        headers=auth(reader_key),
    )
    assert [(item["key"], item["version"]) for item in rest.json()["items"]] == [("ru", 2)]
    assert rest.json()["nextCursor"] is None
    bad = await client.get("/api/v1/calendars", params={"cursor": "e30"}, headers=auth(admin_key))
    assert bad.status_code == 422

    assert (await client.get("/api/v1/calendars")).status_code == 401


async def test_publishing_needs_calendars_write(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["tasks.read"]
    )
    _, writer_key = await create_agent_with_key(
        client, admin_key, name="writer", permissions=["calendars.write"]
    )
    assert (await _publish(client, reader_key, DOCUMENT["spec"])).status_code == 403
    assert (await _publish(client, writer_key, DOCUMENT["spec"])).status_code == 201


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        (_spec(timezone="Mars/Olympus"), "unknown_timezone"),
        (_spec(years=[{"year": 2024}, {"year": 2024}]), "duplicate_calendar_year"),
        (_spec(years=[{"year": 2024, "holidays": ["2025-01-01"]}]), "calendar_date_outside_year"),
    ],
)
async def test_what_the_schema_cannot_check_is_422(
    client: httpx.AsyncClient, spec: dict[str, Any], code: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _publish(client, admin_key, spec)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert (error["code"], error["details"]["code"]) == ("invalid_calendar", code)


async def test_the_shape_is_checked_by_the_contract(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _publish(client, admin_key, _spec(years=[]))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


async def test_a_package_object_installs_like_the_route(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    ctx = AuthContext(
        tenant_id=uuid.UUID(boot["tenant"]["id"]),
        principal_id=uuid.UUID(boot["adminPrincipal"]["id"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset({Permission.CALENDARS_WRITE.value}),
    )
    async with app.state.session_factory() as db:
        view = await install_calendar(db, ctx, DOCUMENT)
        await db.commit()
    assert (view.created, view.row.version) == (True, 1)

    route = await _publish(client, admin_key, DOCUMENT["spec"])
    assert route.status_code == 200, route.text
    assert route.json()["calendarHash"] == view.row.calendar_hash
    assert len(await _published(client, admin_key)) == 1

    async with app.state.session_factory() as db:
        with pytest.raises(AuthorizationError):
            await install_calendar(db, replace(ctx, permissions=frozenset()), DOCUMENT)


async def test_versions_are_immutable(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    assert (await _publish(client, admin_key, DOCUMENT["spec"])).status_code == 201
    for statement in (
        "UPDATE calendars SET spec = '{}'::jsonb",
        "DELETE FROM calendars",
    ):
        with pytest.raises(Exception, match="immutable"), sync_engine.begin() as connection:
            connection.execute(text(statement))


@pytest.fixture
def alembic_config(migrated_database: str) -> Any:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_the_migration_goes_down_and_up(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    alembic_command.downgrade(alembic_config, BEFORE_CALENDARS)
    assert "calendars" not in inspect(sync_engine).get_table_names()
    alembic_command.upgrade(alembic_config, "head")
    assert "calendars" in inspect(sync_engine).get_table_names()
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    assert (await _publish(client, admin_key, DOCUMENT["spec"])).status_code == 201
