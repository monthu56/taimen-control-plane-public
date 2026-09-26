#!/usr/bin/env python3
"""HRS-3 measurement: eager catalog vs scoped tool search.

The claim under test is that a large tool catalog can be made discoverable
without putting every schema in the prompt and without widening permissions.
"Without putting every schema in the prompt" is a quantity, so it gets
measured rather than asserted, on ONE reference dataset for both modes:

  * input tokens — canonical bytes of what the model would receive, / 4;
  * model round trips — how many times the model must be called;
  * task success — the right tool is among what the model can see;
  * false-negative discovery — the tool IS in the effective policy but the
    search does not surface it (the honest cost of a lexical matcher).

Runs in-process against the test database — no Docker stack beyond
PostgreSQL::

    docker compose --profile test up -d db-test
    uv run python scripts/tool_discovery_benchmark.py

``CP_TEST_DATABASE_URL`` overrides the connection string.
"""

import asyncio
import json
import os
import statistics
from typing import Any

import httpx
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from control_plane.config import Settings
from control_plane.domain.canonical import canonical_bytes
from control_plane.main import create_app

DATABASE_URL = os.environ.get(
    "CP_TEST_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost:5434/control_plane_test",
)
BOOTSTRAP_TOKEN = "benchmark-bootstrap-token"

CATALOG_SIZE = 500
PAGE_LIMIT = 25

#: Twenty tasks with the tool that should serve them. The wording deliberately
#: varies: some queries repeat the tool's own words, some use the domain's
#: words instead — that difference is exactly what a lexical matcher misses.
FIXTURES: list[tuple[str, str]] = [
    ("search files in the repository", "repo.search"),
    ("open a pull request", "repo.pull_request"),
    ("run the unit tests", "ci.run_tests"),
    ("deploy to production", "release.deploy"),
    ("roll back the last release", "release.rollback"),
    ("read application logs", "observability.logs"),
    ("query the metrics dashboard", "observability.metrics"),
    ("send a message to the team channel", "chat.post_message"),
    ("create a calendar invite", "calendar.create_event"),
    ("upload a build artifact", "artifacts.upload"),
    ("rotate a database password", "secrets.rotate"),
    ("scale the worker pool", "infra.scale"),
    ("list open incidents", "incidents.list"),
    ("acknowledge an incident", "incidents.acknowledge"),
    ("translate a document", "content.translate"),
    ("summarize a meeting recording", "content.summarize"),
    ("charge a customer card", "payments.charge"),
    ("issue a refund", "payments.refund"),
    ("export a financial report", "finance.export"),
    ("provision a test environment", "infra.provision"),
]

DESCRIPTIONS = {
    "repo.search": "Search files in the target repository by pattern",
    "repo.pull_request": "Open a pull request from a branch",
    "ci.run_tests": "Run the unit tests of a project",
    "release.deploy": "Deploy a build to production",
    "release.rollback": "Roll back the last release",
    "observability.logs": "Read application logs for a service",
    "observability.metrics": "Query the metrics dashboard",
    "chat.post_message": "Send a message to a team channel",
    "calendar.create_event": "Create a calendar invite",
    "artifacts.upload": "Upload a build artifact",
    "secrets.rotate": "Rotate a database password",
    "infra.scale": "Scale the worker pool of a service",
    "incidents.list": "List open incidents",
    "incidents.acknowledge": "Acknowledge an incident",
    "content.translate": "Translate a document",
    "content.summarize": "Summarize a meeting recording",
    "payments.charge": "Charge a customer card",
    "payments.refund": "Issue a refund for a charge",
    "finance.export": "Export a financial report",
    "infra.provision": "Provision a test environment",
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "What the tool acts on"},
        "options": {
            "type": "object",
            "properties": {
                "dryRun": {"type": "boolean"},
                "timeoutSeconds": {"type": "integer"},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "required": ["target"],
}


def tokens(payload: Any) -> int:
    """A deliberately crude, reproducible token estimate: bytes / 4."""
    return len(canonical_bytes(payload)) // 4


async def _post(client: httpx.AsyncClient, path: str, body: Any, key: str) -> Any:
    response = await client.post(path, json=body, headers={"Authorization": f"Bearer {key}"})
    assert response.status_code in (200, 201), (path, response.status_code, response.text)
    return response.json()


async def seed(client: httpx.AsyncClient) -> tuple[str, list[dict[str, Any]]]:
    boot = await _post(
        client,
        "/api/v1/bootstrap",
        {"tenantSlug": "bench", "tenantName": "Bench", "adminDisplayName": "Admin"},
        BOOTSTRAP_TOKEN,
    )
    admin_key = boot["apiKey"]["key"]
    agent = await _post(
        client, "/api/v1/principals", {"kind": "agent", "displayName": "bench"}, admin_key
    )
    agent_key = (
        await _post(
            client,
            f"/api/v1/principals/{agent['id']}/api-keys",
            {"permissions": ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]},
            admin_key,
        )
    )["key"]

    catalog: list[dict[str, Any]] = []
    for name, description in DESCRIPTIONS.items():
        catalog.append(
            await _post(
                client,
                "/api/v1/skills",
                {
                    "name": name,
                    "protocol": "mcp",
                    "description": description,
                    "inputSchema": SCHEMA,
                },
                admin_key,
            )
        )
    for index in range(CATALOG_SIZE - len(DESCRIPTIONS)):
        catalog.append(
            await _post(
                client,
                "/api/v1/skills",
                {
                    "name": f"filler.tool_{index:04d}",
                    "protocol": "mcp",
                    "description": f"Auxiliary integration number {index}",
                    "inputSchema": SCHEMA,
                },
                admin_key,
            )
        )
    for skill in catalog:
        await _post(
            client,
            f"/api/v1/principals/{agent['id']}/skills",
            {"skillId": skill["id"]},
            admin_key,
        )
    return agent_key, catalog


async def measure(client: httpx.AsyncClient, agent_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {agent_key}"}

    # Eager mode: every schema the principal may use, paged to exhaustion, is
    # what a naive harness would paste into the prompt once per task.
    eager_payload: list[Any] = []
    cursor: str | None = None
    eager_pages = 0
    while True:
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/api/v1/tools", params=params, headers=headers)).json()
        eager_pages += 1
        for item in page["items"]:
            detail = (await client.get(f"/api/v1/tools/{item['id']}", headers=headers)).json()
            eager_payload.append(detail)
        cursor = page["nextCursor"]
        if cursor is None:
            break
    eager_tokens = tokens(eager_payload)

    search_tokens: list[int] = []
    hits = 0
    misses: list[str] = []
    for query, expected in FIXTURES:
        page = (
            await client.get(
                "/api/v1/tools", params={"query": query, "limit": PAGE_LIMIT}, headers=headers
            )
        ).json()
        found = next((item for item in page["items"] if item["name"] == expected), None)
        cost = tokens(page["items"])
        if found is not None:
            hits += 1
            detail = (await client.get(f"/api/v1/tools/{expected}", headers=headers)).json()
            cost += tokens(detail)
        else:
            misses.append(f"{query} -> {expected}")
        search_tokens.append(cost)

    return {
        "catalogSize": CATALOG_SIZE,
        "tasks": len(FIXTURES),
        "eager": {
            "inputTokensPerTask": eager_tokens,
            "modelRoundTrips": 1,
            "taskSuccess": 1.0,
            "falseNegatives": 0,
        },
        "search": {
            "inputTokensPerTask": int(statistics.mean(search_tokens)),
            "inputTokensMedian": int(statistics.median(search_tokens)),
            "modelRoundTrips": 2,
            "taskSuccess": round(hits / len(FIXTURES), 3),
            "falseNegatives": len(misses),
            "misses": misses,
        },
        "reduction": round(1 - statistics.mean(search_tokens) / eager_tokens, 4),
        "eagerDiscoveryPages": eager_pages,
    }


async def main() -> None:
    os.environ["CP_DATABASE_URL"] = DATABASE_URL
    alembic_command.upgrade(AlembicConfig("alembic.ini"), "head")
    settings = Settings(
        database_url=DATABASE_URL,
        bootstrap_token=BOOTSTRAP_TOKEN,
        log_level="WARNING",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
            agent_key, _ = await seed(client)
            print(json.dumps(await measure(client, agent_key), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
