"""Shared helpers for API-level tests."""

import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.common import utcnow
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE, SYSTEM_TASK_TYPE_KEY
from control_plane.infrastructure.auth.api_keys import generate_api_key
from control_plane.infrastructure.context_provider.base import ContextProviderError

BOOTSTRAP_TOKEN = "test-bootstrap-token"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def do_bootstrap(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "Admin"},
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_agent_with_key(
    client: httpx.AsyncClient,
    admin_key: str,
    *,
    name: str = "agent-1",
    permissions: list[str] | None = None,
    kind: str = "agent",
) -> tuple[dict[str, Any], str]:
    """Create an agent (or ``kind``) principal + API key; returns (principal, full_key)."""
    response = await client.post(
        "/api/v1/principals",
        json={"kind": kind, "displayName": name},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    principal = response.json()

    response = await client.post(
        f"/api/v1/principals/{principal['id']}/api-keys",
        json={
            "permissions": permissions
            or [
                "sessions.open",
                "tasks.read",
                "tasks.write",
                "tasks.claim",
                "events.read",
                "observations.write",
            ]
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    return principal, response.json()["key"]


async def open_session(
    client: httpx.AsyncClient, key: str, *, client_name: str = "test-agent", **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/sessions",
        json={"clientName": client_name, **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_task(
    client: httpx.AsyncClient, key: str, *, title: str = "Test task", **extra: Any
) -> dict[str, Any]:
    response = await client.post("/api/v1/tasks", json={"title": title, **extra}, headers=auth(key))
    assert response.status_code == 201, response.text
    return response.json()


def backdate_expiry(sync_engine: Engine, table: str, row_id: str, seconds: int = 5) -> None:
    """Force a lease into the past directly in the database."""
    past = utcnow() - timedelta(seconds=seconds)
    with sync_engine.begin() as conn:
        conn.execute(
            text(f"UPDATE {table} SET expires_at = :past WHERE id = :row_id"),
            {"past": past, "row_id": row_id},
        )


def make_tenant_directly(sync_engine: Engine, slug: str) -> tuple[str, str]:
    """Create a second tenant with an admin key directly in the DB.

    Bootstrap is one-time by design, so tenant-isolation tests provision the
    second tenant at the storage level. Returns (tenant_id, api_key).
    """
    generated = generate_api_key()
    tenant_id = str(uuid.uuid4())
    principal_id = str(uuid.uuid4())
    now = utcnow()
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, created_at, updated_at) "
                "VALUES (:id, :slug, :slug, :now, :now)"
            ),
            {"id": tenant_id, "slug": slug, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO principals "
                "(id, tenant_id, kind, display_name, status, metadata, created_at, updated_at) "
                "VALUES (:id, :tenant_id, 'human', :name, 'active', '{}', :now, :now)"
            ),
            {"id": principal_id, "tenant_id": tenant_id, "name": f"{slug}-admin", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO api_keys "
                "(id, tenant_id, principal_id, key_prefix, key_hash, permissions, created_at) "
                "VALUES (:id, :tenant_id, :principal_id, :prefix, :hash, '[\"admin\"]', :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "prefix": generated.prefix,
                "hash": generated.key_hash,
                "now": now,
            },
        )
        # v0.5: every tenant needs its system workspace type before any
        # workspace can be created (bootstrap does this for tenant #1).
        conn.execute(
            text(
                "INSERT INTO workspace_types (id, tenant_id, key, display_name, description,"
                " field_schema, allowed_child_types, is_system, status, version,"
                " created_at, updated_at) VALUES (:id, :tenant_id, 'generic',"
                " 'Generic Workspace', '', '{}', '[\"*\"]', true, 'active', 1, :now, :now)"
            ),
            {"id": str(uuid.uuid4()), "tenant_id": tenant_id, "now": now},
        )
        # v0.8: likewise the system work item type — an unqualified task
        # creation resolves to it (ADR-0048).
        conn.execute(
            text(
                "INSERT INTO task_types (id, tenant_id, key, version, display_name,"
                " description, field_schema, lifecycle_schema, status, created_by,"
                " created_at, updated_at) VALUES (:id, :tenant_id, :key, 1, 'Task', '',"
                " '{}', CAST(:lifecycle AS jsonb), 'active', :created_by, :now, :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "key": SYSTEM_TASK_TYPE_KEY,
                "lifecycle": json.dumps(SYSTEM_TASK_LIFECYCLE),
                "created_by": principal_id,
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO event_journal_floor (tenant_id, journal_tx_id, journal_sequence,"
                " archive_tx_id, archive_sequence, updated_at)"
                " VALUES (:tenant, 0, 0, 0, 0, now()) ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"tenant": tenant_id},
        )
    return tenant_id, generated.full_key


# --- v0.2 helpers -------------------------------------------------------------

ORG_AGENT_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "events.read",
    "artifacts.read",
    "artifacts.write",
    "observations.write",
]


async def create_workspace(
    client: httpx.AsyncClient, key: str, slug: str, *, parent_id: str | None = None, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": slug, "name": slug.title(), "parentId": parent_id, **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_role(
    client: httpx.AsyncClient, key: str, slug: str, *, workspace_id: str | None = None
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/roles",
        json={"slug": slug, "name": slug.title(), "workspaceId": workspace_id},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_capability(client: httpx.AsyncClient, key: str, name: str) -> dict[str, Any]:
    response = await client.post("/api/v1/capabilities", json={"name": name}, headers=auth(key))
    assert response.status_code == 201, response.text
    return response.json()


async def register_skill(
    client: httpx.AsyncClient, key: str, name: str, *, protocol: str = "http", **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/skills", json={"name": name, "protocol": protocol, **extra}, headers=auth(key)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def assign_role(
    client: httpx.AsyncClient,
    key: str,
    principal_id: str,
    role_id: str,
    *,
    workspace_id: str | None = None,
) -> None:
    response = await client.post(
        f"/api/v1/principals/{principal_id}/roles",
        json={"roleId": role_id, "workspaceId": workspace_id},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def assign_capability(
    client: httpx.AsyncClient, key: str, principal_id: str, capability_id: str
) -> None:
    response = await client.post(
        f"/api/v1/principals/{principal_id}/capabilities",
        json={"capabilityId": capability_id},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def assign_skill(
    client: httpx.AsyncClient, key: str, principal_id: str, skill_id: str
) -> None:
    response = await client.post(
        f"/api/v1/principals/{principal_id}/skills",
        json={"skillId": skill_id},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def claim_task(
    client: httpx.AsyncClient, key: str, task_ref: str, session_id: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tasks/{task_ref}:claim",
        json={"sessionId": session_id},
        headers=auth(key),
    )


# Knowledge proxy (CP-ADR-0060): a snapshot and a fake Memory behind the core.
SECRET_TEXT = "entity body that must never reach the journal"


def knowledge_snapshot(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pack": "selfdev",
        "source": "git:platform",
        "scope": "repo:control-plane",
        "snapshotId": "snap-1",
        "observedAt": "2026-09-23T10:00:00Z",
        "entities": [{"kind": "module", "key": "m1", "title": SECRET_TEXT}],
        "relations": [
            {
                "relation": "imports",
                "from": {"kind": "module", "key": "m1"},
                "to": {"kind": "module", "key": "m2"},
            }
        ],
    }
    body.update(overrides)
    return body


class FakeKnowledge:
    """Records what the core forwards to Memory; answers or fails on demand."""

    def __init__(
        self,
        *,
        fail_status: int | None = None,
        retryable: bool = False,
        changes: dict[str, Any] | None = None,
    ) -> None:
        self.fail_status = fail_status
        self.retryable = retryable
        # ``changes`` of Memory's reconcile answer (MEM-ADR-020), when given.
        self.changes = changes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _maybe_fail(self) -> None:
        if self.fail_status is not None:
            raise ContextProviderError(
                f"memory said {self.fail_status}",
                retryable=self.retryable,
                status=self.fail_status,
            )

    async def reconcile_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("reconcile", kwargs))
        self._maybe_fail()
        answer: dict[str, Any] = {
            "snapshotId": kwargs["snapshot"]["snapshotId"],
            "duplicate": False,
            "entities": {"created": 1, "updated": 0, "deleted": 0},
            "relations": {"created": 1, "deleted": 0},
        }
        if self.changes is not None:
            answer["changes"] = self.changes
        return answer

    async def register_package(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("package", kwargs))
        self._maybe_fail()
        package = kwargs["package"]
        return {
            "status": "created",
            "pack": {"name": package["name"], "version": str(package.get("version", ""))},
        }

    async def set_namespace_kinds(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("kinds", kwargs))
        self._maybe_fail()
        return {"namespace": kwargs["namespace"], "packages": kwargs["packages"]}

    async def aclose(self) -> None:
        pass
