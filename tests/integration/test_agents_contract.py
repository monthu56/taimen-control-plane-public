"""The permissions of the ``/agents`` routes (CP-ADR-0073 §5).

Each route checks its own permission before anything else: a caller without
it gets 403, a caller with it gets past the check (the behaviour itself is
pinned in ``test_agent_registry.py``).
"""

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.helpers import auth, create_agent_with_key, do_bootstrap

EXAMPLE = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "fixtures" / "agents" / "coder.yaml").read_text(
        encoding="utf-8"
    )
)
PUBLISH = {"key": EXAMPLE["key"], "spec": EXAMPLE["spec"]}
IDENTITY = {
    "issuer": "https://iam.example.test",
    "iamTenantId": "00000000-0000-4000-8000-000000000001",
    "iamPrincipalId": "00000000-0000-4000-8000-000000000002",
}
REPORT = {
    "phase": "running",
    "instances": {"desired": 1, "ready": 1},
    "observedAt": "2026-09-27T10:00:00+00:00",
}

# (method, path, body, permission the route requires; None — authentication only)
ROUTES: list[tuple[str, str, dict[str, Any] | None, str | None]] = [
    ("POST", "/api/v1/agents", PUBLISH, "agents.manage"),
    ("POST", "/api/v1/agents:validate", PUBLISH, "agents.manage"),
    ("GET", "/api/v1/agents", None, "agents.read"),
    ("GET", "/api/v1/agents?include=status", None, "agents.read"),
    ("GET", "/api/v1/agents/me", None, None),
    ("GET", "/api/v1/agents/coder@1", None, "agents.read"),
    ("PATCH", "/api/v1/agents/coder/state", {"state": "stopped"}, "agents.manage"),
    ("POST", "/api/v1/agents/coder:retire", {"reason": "gone"}, "agents.manage"),
    ("PUT", "/api/v1/agents/coder/identity", IDENTITY, "agents.status.write"),
    (
        "POST",
        "/api/v1/agents/coder/identity:replace",
        {**IDENTITY, "reason": "re-created"},
        "agents.manage",
    ),
    ("GET", "/api/v1/agents/coder/revisions", None, "agents.read"),
    ("GET", "/api/v1/agents/coder/status", None, "agents.read"),
    ("PUT", "/api/v1/agents/coder/status", REPORT, "agents.status.write"),
]


@pytest.mark.parametrize(("method", "path", "body", "permission"), ROUTES)
async def test_route_checks_its_permission(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    permission: str | None,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, outsider_key = await create_agent_with_key(
        client, admin_key, name="outsider", permissions=["tasks.read"]
    )
    _, holder_key = await create_agent_with_key(
        client, admin_key, name="holder", permissions=[permission or "tasks.read"]
    )

    if permission is not None:
        denied = await client.request(method, path, json=body, headers=auth(outsider_key))
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "permission_denied"

    # Past the permission check: the agent does not exist, the caller is none,
    # or the spec describes rights the holder lacks — never a plain denial.
    response = await client.request(method, path, json=body, headers=auth(holder_key))
    assert response.status_code not in (401, 501), response.text
    if response.status_code == 403:
        assert response.json()["error"]["code"] == "permission_escalation"


async def test_the_status_route_is_not_open_to_managers(client: httpx.AsyncClient) -> None:
    """Only the placement service writes what actually runs (§4)."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, manager_key = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["agents.manage", "agents.read"]
    )
    response = await client.put(
        "/api/v1/agents/coder/status", json=REPORT, headers=auth(manager_key)
    )
    assert response.status_code == 403


async def test_an_invalid_spec_is_refused_before_anything_else(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    spec = {**EXAMPLE["spec"], "placement": {"replicas": -1}}
    response = await client.post(
        "/api/v1/agents", json={"key": "coder", "spec": spec}, headers=auth(admin_key)
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
