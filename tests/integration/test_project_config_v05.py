"""Versioned project configuration and the governance lattice (ADR-0032/0033).

Guarantee under test: project configuration is an append-only journal of
revisions that only an explicit activation makes effective; the effective
config is folded on the server in one fixed layer order — template, ancestor,
own revision, profile — with per-key provenance; and a descendant may only ever
tighten the governance its ancestors impose, checked at revision creation, at
activation, and again when a workspace move re-parents it.
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, create_workspace, do_bootstrap


async def _create_template(
    client: httpx.AsyncClient, admin_key: str, key: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/project-templates",
        json={"key": key, "displayName": key.title(), **extra},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_project(
    client: httpx.AsyncClient, admin_key: str, workspace_id: str, template_id: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/projects",
        json={"workspaceId": workspace_id, "templateId": template_id, **extra},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _post_revision(
    client: httpx.AsyncClient, admin_key: str, project_id: str, config: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{project_id}/config-revisions",
        json={"config": config},
        headers=auth(admin_key),
    )


async def _create_revision(
    client: httpx.AsyncClient, admin_key: str, project_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    response = await _post_revision(client, admin_key, project_id, config)
    assert response.status_code == 201, response.text
    return response.json()


async def _activate(
    client: httpx.AsyncClient, admin_key: str, project_id: str, revision: int, version: int
) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{project_id}/config-revisions/{revision}:activate",
        headers={**auth(admin_key), "If-Match": f'"project-{version}"'},
    )


async def _activate_ok(
    client: httpx.AsyncClient, admin_key: str, project_id: str, revision: int, version: int
) -> dict[str, Any]:
    response = await _activate(client, admin_key, project_id, revision, version)
    assert response.status_code == 200, response.text
    return response.json()


async def _configure(
    client: httpx.AsyncClient, admin_key: str, project: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Create one revision and activate it — the common two-step in one call."""
    revision = await _create_revision(client, admin_key, project["id"], config)
    return await _activate_ok(
        client, admin_key, project["id"], revision["revision"], project["version"]
    )


async def _effective(client: httpx.AsyncClient, admin_key: str, project_id: str) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/projects/{project_id}/effective-config", headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sources(effective: dict[str, Any], section: str = "settings") -> dict[str, str]:
    return {key: origin["source"] for key, origin in effective["provenance"][section].items()}


async def _project_events(
    client: httpx.AsyncClient, admin_key: str, project_id: str, event_type: str
) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events",
        params={"entityType": "project", "entityId": project_id, "limit": 200},
        headers=auth(admin_key),
    )
    assert response.status_code == 200, response.text
    return [event for event in response.json()["items"] if event["type"] == event_type]


# --- append-only revision journal ---------------------------------------------


async def test_creating_revisions_is_monotonic_and_does_not_activate(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "acme")
    template = await _create_template(client, admin_key, "delivery")
    project = await _create_project(client, admin_key, workspace["id"], template["id"])

    revisions = [
        await _create_revision(client, admin_key, project["id"], {"settings": {"tone": tone}})
        for tone in ("formal", "terse", "playful")
    ]
    assert [r["revision"] for r in revisions] == [1, 2, 3]
    assert [r["activatedAt"] for r in revisions] == [None, None, None]
    # The revision records what it was validated against, for audit.
    assert revisions[0]["validation"]["templateKey"] == "delivery"

    response = await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    body = response.json()
    assert body["activeConfigRevision"] is None
    assert body["activeConfigRevisionId"] is None
    # Writing to the journal is not a change to the project itself.
    assert body["version"] == 1
    assert response.headers["etag"] == '"project-1"'

    listed = (
        await client.get(
            f"/api/v1/projects/{project['id']}/config-revisions", headers=auth(admin_key)
        )
    ).json()
    assert sorted(r["revision"] for r in listed["items"]) == [1, 2, 3]

    # Nothing in the journal reached the effective config either.
    effective = await _effective(client, admin_key, project["id"])
    assert effective["config"]["settings"] == {}
    assert effective["provenance"]["layers"][-1]["revision"] is None


async def test_revisions_are_append_only_in_the_database(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "acme")
    template = await _create_template(client, admin_key, "delivery")
    project = await _create_project(client, admin_key, workspace["id"], template["id"])
    revision = await _create_revision(
        client, admin_key, project["id"], {"settings": {"tone": "formal"}}
    )

    with sync_engine.connect() as conn, pytest.raises(Exception, match="immutable"):
        conn.execute(text("UPDATE project_config_revisions SET config = '{}'::jsonb"))
    with sync_engine.connect() as conn, pytest.raises(Exception, match="immutable"):
        conn.execute(text("UPDATE project_config_revisions SET revision = 99"))
    with sync_engine.connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM project_config_revisions"))

    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT config, revision FROM project_config_revisions WHERE id = :id"),
            {"id": revision["id"]},
        ).one()
    assert stored.revision == 1
    assert stored.config["settings"] == {"tone": "formal"}


# --- activation ---------------------------------------------------------------


async def test_activation_moves_the_pointer_bumps_version_and_emits_an_event(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "acme")
    template = await _create_template(client, admin_key, "delivery")
    project = await _create_project(client, admin_key, workspace["id"], template["id"])
    first = await _create_revision(
        client, admin_key, project["id"], {"settings": {"tone": "formal"}}
    )
    await _create_revision(client, admin_key, project["id"], {"settings": {"tone": "terse"}})

    # If-Match is mandatory on activation.
    missing = await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions/1:activate", headers=auth(admin_key)
    )
    assert missing.status_code == 428

    activated = await _activate_ok(client, admin_key, project["id"], 1, 1)
    assert activated["version"] == 2
    assert activated["activeConfigRevision"] == 1
    assert activated["activeConfigRevisionId"] == first["id"]

    listed = (
        await client.get(
            f"/api/v1/projects/{project['id']}/config-revisions", headers=auth(admin_key)
        )
    ).json()["items"]
    by_revision = {r["revision"]: r for r in listed}
    assert by_revision[1]["activatedAt"] is not None
    assert by_revision[2]["activatedAt"] is None

    events = await _project_events(
        client, admin_key, project["id"], "project.config_revision_activated"
    )
    assert len(events) == 1
    assert events[0]["payload"] == {"revision": 1, "revisionId": first["id"], "version": 2}

    # A revision that does not exist is a 404, not a validation error.
    assert (await _activate(client, admin_key, project["id"], 99, 2)).status_code == 404

    # A stale If-Match loses to the version the activation above produced.
    stale = await _activate(client, admin_key, project["id"], 2, 1)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"

    still_first = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    ).json()
    assert still_first["activeConfigRevision"] == 1
    assert still_first["version"] == 2


async def test_activation_replays_under_the_same_idempotency_key(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "acme")
    template = await _create_template(client, admin_key, "delivery")
    project = await _create_project(client, admin_key, workspace["id"], template["id"])
    await _create_revision(client, admin_key, project["id"], {"settings": {"tone": "formal"}})

    url = f"/api/v1/projects/{project['id']}/config-revisions/1:activate"
    headers = {**auth(admin_key), "If-Match": '"project-1"', "Idempotency-Key": str(uuid.uuid4())}

    first = await client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    assert "idempotency-replayed" not in first.headers

    second = await client.post(url, headers=headers)
    assert second.status_code == 200, second.text
    assert second.headers.get("idempotency-replayed") == "true"
    assert second.json() == first.json()

    # The replay is a replay: one version bump, one event.
    current = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    ).json()
    assert current["version"] == 2
    events = await _project_events(
        client, admin_key, project["id"], "project.config_revision_activated"
    )
    assert len(events) == 1


# --- effective config ---------------------------------------------------------


async def test_effective_config_layer_order_and_provenance(client: httpx.AsyncClient) -> None:
    """template -> ancestor -> revision -> profile, with a source per key."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    parent_ws = await create_workspace(client, admin_key, "portfolio")
    child_ws = await create_workspace(client, admin_key, "delivery", parent_id=parent_ws["id"])

    parent_template = await _create_template(
        client,
        admin_key,
        "portfolio",
        defaultConfig={
            "settings": {"portfolioSetting": "parent-template", "layered": "parent-template"}
        },
    )
    child_template = await _create_template(
        client,
        admin_key,
        "delivery",
        defaultConfig={
            "settings": {"templateSetting": "child-template", "layered": "child-template"}
        },
    )

    parent = await _create_project(client, admin_key, parent_ws["id"], parent_template["id"])
    await _configure(
        client,
        admin_key,
        parent,
        {"settings": {"ancestorSetting": "parent-revision", "layered": "parent-revision"}},
    )

    child = await _create_project(
        client,
        admin_key,
        child_ws["id"],
        child_template["id"],
        settings={"profileSetting": "child-profile", "layered": "child-profile"},
    )
    await _configure(
        client,
        admin_key,
        child,
        {"settings": {"revisionSetting": "child-revision", "layered": "child-revision"}},
    )

    effective = await _effective(client, admin_key, child["id"])
    assert effective["config"]["settings"] == {
        "templateSetting": "child-template",
        "portfolioSetting": "parent-template",
        "ancestorSetting": "parent-revision",
        "revisionSetting": "child-revision",
        "profileSetting": "child-profile",
        # Set by all four layers: the last one in the order wins.
        "layered": "child-profile",
    }
    assert _sources(effective) == {
        "templateSetting": "template",
        "portfolioSetting": "ancestor",
        "ancestorSetting": "ancestor",
        "revisionSetting": "revision",
        "profileSetting": "profile",
        "layered": "profile",
    }

    provenance = effective["provenance"]["settings"]
    assert provenance["templateSetting"]["templateKey"] == "delivery"
    # An inherited key names the ancestor project it came from, not the child.
    assert provenance["portfolioSetting"]["projectId"] == parent["id"]
    assert provenance["ancestorSetting"]["revision"] == 1
    assert provenance["revisionSetting"]["projectId"] == child["id"]
    assert provenance["revisionSetting"]["revision"] == 1

    # Layers are ordered by depth in the workspace tree, root-most first.
    layers = effective["provenance"]["layers"]
    assert [layer["projectId"] for layer in layers] == [parent["id"], child["id"]]
    assert [layer["depth"] for layer in layers] == [0, 1]


async def test_deep_merge_merges_objects_but_replaces_arrays_and_scalars(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    parent_ws = await create_workspace(client, admin_key, "portfolio")
    child_ws = await create_workspace(client, admin_key, "delivery", parent_id=parent_ws["id"])
    template = await _create_template(client, admin_key, "delivery")

    parent = await _create_project(client, admin_key, parent_ws["id"], template["id"])
    await _configure(
        client,
        admin_key,
        parent,
        {
            "settings": {
                "nested": {"keep": "parent", "override": "parent", "deep": {"x": 1, "y": 2}},
                "list": [1, 2],
                "scalar": "parent",
            }
        },
    )

    child = await _create_project(client, admin_key, child_ws["id"], template["id"])
    await _configure(
        client,
        admin_key,
        child,
        {
            "settings": {
                "nested": {"override": "child", "deep": {"y": 20, "z": 30}, "added": True},
                "list": [9],
                "scalar": "child",
            }
        },
    )

    settings = (await _effective(client, admin_key, child["id"]))["config"]["settings"]
    assert settings == {
        # Objects merge recursively at every level.
        "nested": {
            "keep": "parent",
            "override": "child",
            "deep": {"x": 1, "y": 20, "z": 30},
            "added": True,
        },
        # Arrays and scalars are replaced whole.
        "list": [9],
        "scalar": "child",
    }


# --- governance lattice -------------------------------------------------------

_ANCESTOR_GOVERNANCE: dict[str, Any] = {
    "maxRunActions": 100,
    "maxRunDurationSeconds": 600,
    "maxConcurrentRuns": 4,
    "requireApprovalForRun": True,
    "allowedSkillProtocols": ["http", "mcp"],
    "maxAutonomyLevel": "assisted",
}


async def _governed_pair(
    client: httpx.AsyncClient, admin_key: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A parent project with governance plus an unconfigured child under it."""
    parent_ws = await create_workspace(client, admin_key, "portfolio")
    child_ws = await create_workspace(client, admin_key, "delivery", parent_id=parent_ws["id"])
    template = await _create_template(client, admin_key, "delivery")
    parent = await _create_project(client, admin_key, parent_ws["id"], template["id"])
    await _configure(client, admin_key, parent, {"governance": _ANCESTOR_GOVERNANCE})
    child = await _create_project(client, admin_key, child_ws["id"], template["id"])
    return parent, child


async def test_a_child_revision_may_not_weaken_ancestor_governance(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, child = await _governed_pair(client, admin_key)

    weakenings: list[tuple[dict[str, Any], str]] = [
        ({"maxRunActions": 500}, "/governance/maxRunActions"),
        ({"maxRunDurationSeconds": None}, "/governance/maxRunDurationSeconds"),
        ({"allowedSkillProtocols": ["http", "mcp", "local"]}, "/governance/allowedSkillProtocols"),
        ({"requireApprovalForRun": False}, "/governance/requireApprovalForRun"),
        ({"maxAutonomyLevel": "autonomous"}, "/governance/maxAutonomyLevel"),
    ]
    for governance, path in weakenings:
        response = await _post_revision(client, admin_key, child["id"], {"governance": governance})
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "governance_weakened"
        assert [v["path"] for v in error["details"]["violations"]] == [path]

    # Nothing was written: the journal is still empty.
    listed = (
        await client.get(
            f"/api/v1/projects/{child['id']}/config-revisions", headers=auth(admin_key)
        )
    ).json()
    assert listed["items"] == []


async def test_a_child_revision_may_tighten_and_effective_governance_is_the_stricter_fold(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    parent, child = await _governed_pair(client, admin_key)

    await _configure(
        client,
        admin_key,
        child,
        {
            "governance": {
                "maxRunActions": 50,
                "requireApprovalForRun": True,
                "allowedSkillProtocols": ["mcp"],
                "maxAutonomyLevel": "supervised",
            }
        },
    )

    effective = await _effective(client, admin_key, child["id"])
    assert effective["config"]["governance"] == {
        "maxRunActions": 50,
        "maxRunDurationSeconds": 600,
        "maxConcurrentRuns": 4,
        "requireApprovalForRun": True,
        "allowedSkillProtocols": ["mcp"],
        "maxAutonomyLevel": "supervised",
    }
    assert _sources(effective, "governance") == {
        # Tightened here.
        "maxRunActions": "revision",
        "allowedSkillProtocols": "revision",
        "maxAutonomyLevel": "revision",
        # Untouched, so still attributed to the ancestor that set them.
        "maxRunDurationSeconds": "ancestor",
        "maxConcurrentRuns": "ancestor",
        "requireApprovalForRun": "ancestor",
    }

    # The ancestor itself is unaffected by what its descendant did.
    parent_effective = await _effective(client, admin_key, parent["id"])
    assert parent_effective["config"]["governance"] == _ANCESTOR_GOVERNANCE


async def test_unknown_governance_field_is_rejected(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "acme")
    template = await _create_template(client, admin_key, "delivery")
    project = await _create_project(client, admin_key, workspace["id"], template["id"])

    response = await _post_revision(
        client, admin_key, project["id"], {"governance": {"maxBudgetUsd": 10}}
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unknown_governance_field"
    assert error["details"]["path"] == "/maxBudgetUsd"
    assert "maxRunActions" in error["details"]["known"]

    # The same closed vocabulary guards a template's default config.
    response = await client.post(
        "/api/v1/project-templates",
        json={
            "key": "rogue",
            "displayName": "Rogue",
            "defaultConfig": {"governance": {"policyDsl": "allow *"}},
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_governance_field"


# --- locked settings ----------------------------------------------------------


async def test_a_setting_locked_by_an_ancestor_cannot_be_overridden(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    parent_ws = await create_workspace(client, admin_key, "portfolio")
    child_ws = await create_workspace(client, admin_key, "delivery", parent_id=parent_ws["id"])
    template = await _create_template(client, admin_key, "delivery")

    parent = await _create_project(client, admin_key, parent_ws["id"], template["id"])
    await _configure(
        client,
        admin_key,
        parent,
        {
            "settings": {"tone": "formal", "locale": "en"},
            "inheritance": {"lockedSettings": ["tone"]},
        },
    )
    child = await _create_project(client, admin_key, child_ws["id"], template["id"])

    locked = await _post_revision(client, admin_key, child["id"], {"settings": {"tone": "casual"}})
    assert locked.status_code == 422, locked.text
    assert locked.json()["error"]["code"] == "setting_locked"
    assert locked.json()["error"]["details"]["lockedSettings"] == ["tone"]

    # The profile overlay is guarded by the same rule.
    patched = await client.patch(
        f"/api/v1/projects/{child['id']}",
        json={"settings": {"tone": "casual"}},
        headers={**auth(admin_key), "If-Match": '"project-1"'},
    )
    assert patched.status_code == 422
    assert patched.json()["error"]["code"] == "setting_locked"

    # An unlocked key next to it is still the child's to set.
    assert (
        await _post_revision(client, admin_key, child["id"], {"settings": {"locale": "de"}})
    ).status_code == 201

    effective = await _effective(client, admin_key, child["id"])
    assert effective["config"]["settings"]["tone"] == "formal"
    assert effective["provenance"]["lockedSettings"] == ["tone"]


# --- secret material ----------------------------------------------------------


async def test_secret_material_is_rejected_but_secret_refs_are_accepted(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "acme")
    template = await _create_template(client, admin_key, "delivery")
    project = await _create_project(client, admin_key, workspace["id"], template["id"])

    flat = await _post_revision(client, admin_key, project["id"], {"settings": {"apiKey": "sk-1"}})
    assert flat.status_code == 422, flat.text
    assert flat.json()["error"]["code"] == "secret_material_rejected"
    assert flat.json()["error"]["details"]["path"] == "settings.apiKey"

    nested = await _post_revision(
        client, admin_key, project["id"], {"memory": {"store": {"password": "hunter2"}}}
    )
    assert nested.status_code == 422
    assert nested.json()["error"]["details"]["path"] == "memory.store.password"

    reference = await client.post(
        f"/api/v1/projects/{project['id']}/external-references",
        json={
            "externalSystem": "jira",
            "externalType": "board",
            "externalId": "ACME-1",
            "metadata": {"apiKey": "sk-2"},
        },
        headers=auth(admin_key),
    )
    assert reference.status_code == 422
    assert reference.json()["error"]["code"] == "secret_material_rejected"

    # An opaque pointer is the sanctioned way to say "there is a secret".
    assert (
        await _post_revision(
            client, admin_key, project["id"], {"settings": {"secretRef": "vault://acme/token"}}
        )
    ).status_code == 201
    assert (
        await client.post(
            f"/api/v1/projects/{project['id']}/external-references",
            json={
                "externalSystem": "jira",
                "externalType": "board",
                "externalId": "ACME-1",
                "metadata": {"secretRef": "vault://acme/jira"},
            },
            headers=auth(admin_key),
        )
    ).status_code == 201


# --- governance across a workspace move ---------------------------------------


async def test_a_move_that_would_weaken_governance_is_rejected_whole(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    template = await _create_template(client, admin_key, "delivery")

    strict_ws = await create_workspace(client, admin_key, "strict")
    strict = await _create_project(client, admin_key, strict_ws["id"], template["id"])
    await _configure(client, admin_key, strict, {"governance": {"maxRunActions": 10}})

    loose_ws = await create_workspace(client, admin_key, "loose")
    loose = await _create_project(client, admin_key, loose_ws["id"], template["id"])
    # Legal at the root: with no ancestors there is no ceiling to weaken.
    await _configure(client, admin_key, loose, {"governance": {"maxRunActions": 100}})

    response = await client.post(
        f"/api/v1/workspaces/{loose_ws['id']}:move",
        json={"newParentId": strict_ws["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "governance_weakened"
    assert error["details"]["projectId"] == loose["id"]
    assert [v["path"] for v in error["details"]["violations"]] == ["/governance/maxRunActions"]

    # The hierarchy is exactly as it was — no partially applied move.
    after = (
        await client.get(f"/api/v1/workspaces/{loose_ws['id']}", headers=auth(admin_key))
    ).json()
    assert after["parentId"] is None
    assert after["version"] == loose_ws["version"]
    assert (await client.get(f"/api/v1/projects/{loose['id']}", headers=auth(admin_key))).json()[
        "parentProjectId"
    ] is None

    # A project that already fits the new ceiling moves without complaint.
    tight_ws = await create_workspace(client, admin_key, "tight")
    tight = await _create_project(client, admin_key, tight_ws["id"], template["id"])
    await _configure(client, admin_key, tight, {"governance": {"maxRunActions": 5}})
    moved = await client.post(
        f"/api/v1/workspaces/{tight_ws['id']}:move",
        json={"newParentId": strict_ws["id"]},
        headers=auth(admin_key),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["parentId"] == strict_ws["id"]
    effective = await _effective(client, admin_key, tight["id"])
    assert effective["config"]["governance"]["maxRunActions"] == 5


# --- external references ------------------------------------------------------


async def test_external_reference_identity_is_unique_and_immutable(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    template = await _create_template(client, admin_key, "delivery")
    first_ws = await create_workspace(client, admin_key, "first")
    second_ws = await create_workspace(client, admin_key, "second")
    first = await _create_project(client, admin_key, first_ws["id"], template["id"])
    second = await _create_project(client, admin_key, second_ws["id"], template["id"])

    triple = {"externalSystem": "jira", "externalType": "board", "externalId": "ACME-1"}

    created = await client.post(
        f"/api/v1/projects/{first['id']}/external-references",
        json=triple,
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    assert created.json()["version"] == 1
    assert created.json()["metadata"] == {}

    updated = await client.post(
        f"/api/v1/projects/{first['id']}/external-references",
        json={**triple, "metadata": {"url": "https://jira.example/ACME-1"}},
        headers=auth(admin_key),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == created.json()["id"]
    assert updated.json()["metadata"] == {"url": "https://jira.example/ACME-1"}
    assert updated.json()["version"] == 2

    conflict = await client.post(
        f"/api/v1/projects/{second['id']}/external-references",
        json=triple,
        headers=auth(admin_key),
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "external_reference_conflict"
    assert conflict.json()["error"]["details"]["entityId"] == first["id"]

    found = (
        await client.get(
            "/api/v1/projects",
            params={"externalSystem": "jira", "externalId": "ACME-1"},
            headers=auth(admin_key),
        )
    ).json()
    assert [p["id"] for p in found["items"]] == [first["id"]]

    # The mapping is a lookup key, so the system must be named with the id.
    incomplete = await client.get(
        "/api/v1/projects", params={"externalId": "ACME-1"}, headers=auth(admin_key)
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "invalid_external_lookup"

    with sync_engine.connect() as conn, pytest.raises(Exception, match="immutable"):
        conn.execute(text("UPDATE external_references SET external_id = 'ACME-2'"))
    with sync_engine.connect() as conn, pytest.raises(Exception, match="immutable"):
        conn.execute(text("UPDATE external_references SET entity_id = :id"), {"id": second["id"]})


# --- authorization ------------------------------------------------------------


async def test_reading_config_is_separate_from_reshaping_it(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "acme")
    template = await _create_template(client, admin_key, "delivery")
    project = await _create_project(client, admin_key, workspace["id"], template["id"])
    await _create_revision(client, admin_key, project["id"], {"settings": {"tone": "formal"}})

    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["projects.read"]
    )
    _, stranger_key = await create_agent_with_key(
        client, admin_key, name="stranger", permissions=["tasks.read"]
    )

    assert (
        await client.get(
            f"/api/v1/projects/{project['id']}/effective-config", headers=auth(reader_key)
        )
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/projects/{project['id']}/config-revisions", headers=auth(reader_key)
        )
    ).status_code == 200
    assert (
        await _post_revision(client, reader_key, project["id"], {"settings": {"tone": "terse"}})
    ).status_code == 403
    assert (await _activate(client, reader_key, project["id"], 1, 1)).status_code == 403

    assert (
        await client.get(
            f"/api/v1/projects/{project['id']}/effective-config", headers=auth(stranger_key)
        )
    ).status_code == 403
