"""Project Templates and project lifecycle (ADR-0030, ADR-0031).

The guarantee under test: a template version is allocated by the server and is
immutable from the moment it is written — the only mutation anyone (including
raw SQL) may perform is ``active -> deprecated``. Editing a template therefore
always means "create the next version", a deprecated version stops accepting
new projects while existing ones keep working, and a project may only move
between statuses its own template version declared.
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.helpers import auth, create_agent_with_key, do_bootstrap

# A deliberately non-default lifecycle: user-facing keys the core has never
# heard of, mapped onto the five system categories.
DELIVERY_LIFECYCLE: dict[str, Any] = {
    "initialStatus": "discovery",
    "statuses": [
        {"key": "discovery", "displayName": "Discovery", "category": "planned"},
        {"key": "pilot", "displayName": "Pilot", "category": "active"},
        {"key": "production", "displayName": "Production", "category": "active"},
        {"key": "cancelled", "displayName": "Cancelled", "category": "terminal_cancelled"},
    ],
    "transitions": [
        {"from": "discovery", "to": ["pilot", "cancelled"]},
        {"from": "pilot", "to": ["production", "cancelled"]},
    ],
}

_TEMPLATE_COLUMNS_AFTER_ID = (
    "tenant_id, key, version, display_name, description, field_schema, "
    "lifecycle_schema, default_config, default_views, governance_schema, "
    "memory_defaults, status, created_by, created_at, updated_at"
)
CLONE_TEMPLATE_SQL = (
    f"INSERT INTO project_templates (id, {_TEMPLATE_COLUMNS_AFTER_ID}) "
    f"SELECT :new_id, {_TEMPLATE_COLUMNS_AFTER_ID} "
    "FROM project_templates WHERE id = :id"
)


def execute_sql(sync_engine: Engine, statement: str, **params: Any) -> None:
    """Run one statement in its own transaction (immutability lives in the DB)."""
    with sync_engine.begin() as conn:
        conn.execute(text(statement), params)


async def create_template(
    client: httpx.AsyncClient, key: str, template_key: str = "delivery", **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/project-templates",
        json={"key": template_key, "displayName": template_key.title(), **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_project(
    client: httpx.AsyncClient, key: str, slug: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/projects",
        json={"workspaceSlug": slug, "workspaceName": slug.title(), **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_server_allocates_monotonic_template_versions(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    first = await create_template(client, admin_key, "delivery")
    second = await create_template(client, admin_key, "delivery", description="second cut")
    other = await create_template(client, admin_key, "research")

    # The version is server-allocated and carried in the create response.
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["id"] != second["id"]
    assert first["key"] == second["key"] == "delivery"
    # Version numbering is per (tenant, key), not global.
    assert other["version"] == 1

    # ...and it is a real database invariant, not an application convention:
    # re-inserting the same (tenant, key, version) triple is rejected.
    with pytest.raises(IntegrityError) as excinfo:
        execute_sql(sync_engine, CLONE_TEMPLATE_SQL, new_id=str(uuid.uuid4()), id=second["id"])
    assert "uq_project_templates_key_version" in str(excinfo.value)


async def test_template_version_is_immutable_in_the_database(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    template = await create_template(client, admin_key, "delivery")

    def update(assignment: str) -> None:
        execute_sql(
            sync_engine,
            f"UPDATE project_templates SET {assignment} WHERE id = :id",
            id=template["id"],
        )

    # Content columns are frozen by the trigger — the API has no route for
    # this at all, so the check has to happen at the storage level.
    for assignment in (
        "display_name = 'Renamed'",
        'field_schema = \'{"type": "object"}\'::jsonb',
        "lifecycle_schema = '{}'::jsonb",
        "version = 99",
    ):
        with pytest.raises(DBAPIError) as excinfo:
            update(assignment)
        assert "project_templates content is immutable" in str(excinfo.value)

    with pytest.raises(DBAPIError) as excinfo:
        execute_sql(sync_engine, "DELETE FROM project_templates WHERE id = :id", id=template["id"])
    assert "project_templates rows are immutable" in str(excinfo.value)

    # The single permitted mutation: active -> deprecated. Not the reverse.
    update("status = 'deprecated'")
    with pytest.raises(DBAPIError) as excinfo:
        update("status = 'active'")
    assert "status may only move active -> deprecated" in str(excinfo.value)


async def test_deprecated_template_blocks_new_projects_only(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    template = await create_template(
        client, admin_key, "delivery", lifecycleSchema=DELIVERY_LIFECYCLE
    )
    project = await create_project(client, admin_key, "alpha", templateId=template["id"])

    response = await client.post(
        f"/api/v1/project-templates/{template['id']}:deprecate", headers=auth(admin_key)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deprecated"

    # Idempotent: deprecating twice is a no-op, not a conflict.
    response = await client.post(
        f"/api/v1/project-templates/{template['id']}:deprecate", headers=auth(admin_key)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deprecated"

    # No new project may be created against it.
    response = await client.post(
        "/api/v1/projects",
        json={"workspaceSlug": "beta", "workspaceName": "Beta", "templateId": template["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "template_deprecated"

    # The project already on that version keeps working: it is readable and
    # can still move through the lifecycle the deprecated version declared.
    response = await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    assert response.status_code == 200
    assert response.json()["templateVersion"] == 1

    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "pilot"},
        headers={**auth(admin_key), "If-Match": '"project-1"'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["statusKey"] == "pilot"


async def test_template_listing_filters_and_paginates(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    alpha_v1 = await create_template(client, admin_key, "alpha")
    await create_template(client, admin_key, "alpha")
    await create_template(client, admin_key, "beta")

    response = await client.get("/api/v1/project-templates?key=alpha", headers=auth(admin_key))
    assert response.status_code == 200
    page = response.json()
    assert set(page) == {"items", "nextCursor"}
    assert page["nextCursor"] is None
    assert sorted(t["version"] for t in page["items"]) == [1, 2]
    assert {t["key"] for t in page["items"]} == {"alpha"}

    assert (
        await client.post(
            f"/api/v1/project-templates/{alpha_v1['id']}:deprecate", headers=auth(admin_key)
        )
    ).status_code == 200

    response = await client.get(
        "/api/v1/project-templates?key=alpha&status=deprecated", headers=auth(admin_key)
    )
    assert [t["id"] for t in response.json()["items"]] == [alpha_v1["id"]]

    response = await client.get("/api/v1/project-templates?status=active", headers=auth(admin_key))
    assert len(response.json()["items"]) == 2

    # Cursor pagination walks every version exactly once and ends on a null cursor.
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = (
            await client.get("/api/v1/project-templates", params=params, headers=auth(admin_key))
        ).json()
        assert len(page["items"]) <= 2
        seen.extend(t["id"] for t in page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(seen) == 3
    assert len(set(seen)) == 3


async def test_lifecycle_schema_validation_reports_a_json_path(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    cases: list[tuple[str, dict[str, Any], str]] = [
        (
            "duplicate status keys",
            {
                "initialStatus": "draft",
                "statuses": [
                    {"key": "draft", "category": "planned"},
                    {"key": "draft", "category": "active"},
                ],
            },
            "/statuses/1/key",
        ),
        (
            "unknown system category",
            {
                "initialStatus": "draft",
                "statuses": [{"key": "draft", "category": "in_flight"}],
            },
            "/statuses/0/category",
        ),
        (
            "missing initialStatus",
            {"statuses": [{"key": "draft", "category": "planned"}]},
            "/initialStatus",
        ),
        (
            "transition to an undeclared status",
            {
                "initialStatus": "draft",
                "statuses": [{"key": "draft", "category": "planned"}],
                "transitions": [{"from": "draft", "to": ["ghost"]}],
            },
            "/transitions/0/to/0",
        ),
    ]

    for label, lifecycle, path in cases:
        response = await client.post(
            "/api/v1/project-templates",
            json={"key": "broken", "displayName": "Broken", "lifecycleSchema": lifecycle},
            headers=auth(admin_key),
        )
        assert response.status_code == 422, f"{label}: {response.text}"
        error = response.json()["error"]
        assert error["code"] == "invalid_lifecycle_schema", label
        assert error["details"]["field"] == "lifecycleSchema", label
        assert error["details"]["path"] == path, label

    # Nothing broken was stored.
    page = (await client.get("/api/v1/project-templates", headers=auth(admin_key))).json()
    assert page["items"] == []


async def test_project_transition_follows_the_template_lifecycle(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    template = await create_template(
        client, admin_key, "delivery", lifecycleSchema=DELIVERY_LIFECYCLE
    )
    project = await create_project(client, admin_key, "alpha", templateId=template["id"])

    # The initial status comes from the template, and so does its category.
    assert project["statusKey"] == "discovery"
    assert project["systemStatusCategory"] == "planned"
    assert project["version"] == 1

    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "pilot", "comment": "kickoff"},
        headers={**auth(admin_key), "If-Match": '"project-1"'},
    )
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved["statusKey"] == "pilot"
    assert moved["systemStatusCategory"] == "active"
    assert moved["version"] == 2

    # An undeclared transition is rejected and names the legal targets.
    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "discovery"},
        headers={**auth(admin_key), "If-Match": '"project-2"'},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_transition"
    assert error["details"]["from"] == "pilot"
    assert error["details"]["allowed"] == ["cancelled", "production"]

    # Transitioning to the current status is not a no-op, it is an error.
    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "pilot"},
        headers={**auth(admin_key), "If-Match": '"project-2"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_transition"

    # A status no lifecycle declares is a different failure entirely.
    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "shipped"},
        headers={**auth(admin_key), "If-Match": '"project-2"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "status_not_in_lifecycle"

    # Optimistic concurrency: If-Match is mandatory and must be current.
    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "production"},
        headers=auth(admin_key),
    )
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "if_match_required"

    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "production"},
        headers={**auth(admin_key), "If-Match": '"project-1"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "version_conflict"

    # The rejected attempts left the project exactly where it was.
    response = await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    assert response.json()["statusKey"] == "pilot"
    assert response.headers["etag"] == '"project-2"'


async def test_transition_emits_a_status_changed_event(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    template = await create_template(
        client, admin_key, "delivery", lifecycleSchema=DELIVERY_LIFECYCLE
    )
    project = await create_project(client, admin_key, "alpha", templateId=template["id"])

    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "pilot", "comment": "kickoff"},
        headers={**auth(admin_key), "If-Match": '"project-1"'},
    )
    assert response.status_code == 200, response.text

    events = (
        await client.get(
            f"/api/v1/events?entityType=project&entityId={project['id']}", headers=auth(admin_key)
        )
    ).json()["items"]
    changed = next(e for e in events if e["type"] == "project.status_changed")
    assert changed["entityId"] == project["id"]
    assert changed["payload"]["fromStatusKey"] == "discovery"
    assert changed["payload"]["fromSystemStatusCategory"] == "planned"
    assert changed["payload"]["statusKey"] == "pilot"
    assert changed["payload"]["systemStatusCategory"] == "active"
    assert changed["payload"]["comment"] == "kickoff"
    assert changed["payload"]["version"] == 2


async def test_project_template_permission_matrix(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    template = await create_template(client, admin_key, "delivery")

    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["project_templates.read"]
    )
    _, stranger_key = await create_agent_with_key(
        client, admin_key, name="stranger", permissions=["tasks.read"]
    )

    # project_templates.read is enough to look, never enough to write.
    response = await client.get("/api/v1/project-templates", headers=auth(reader_key))
    assert response.status_code == 200
    assert [t["id"] for t in response.json()["items"]] == [template["id"]]

    response = await client.get(
        f"/api/v1/project-templates/{template['id']}", headers=auth(reader_key)
    )
    assert response.status_code == 200

    response = await client.post(
        "/api/v1/project-templates",
        json={"key": "sneaky", "displayName": "Sneaky"},
        headers=auth(reader_key),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert response.json()["error"]["details"]["required"] == ["project_templates.manage"]

    response = await client.post(
        f"/api/v1/project-templates/{template['id']}:deprecate", headers=auth(reader_key)
    )
    assert response.status_code == 403

    # No template permission at all -> cannot even read the catalogue.
    assert (
        await client.get("/api/v1/project-templates", headers=auth(stranger_key))
    ).status_code == 403
    response = await client.get(
        f"/api/v1/project-templates/{template['id']}", headers=auth(stranger_key)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
