"""Shared test fixtures.

Integration/concurrency/e2e tests run against a real PostgreSQL
(``docker compose --profile test up -d db-test`` or CP_TEST_DATABASE_URL).
Every pytest process works in its own database created next to the one in
CP_TEST_DATABASE_URL (``<name>_<epoch>_<hex>``) and dropped at the end of the
session, so parallel runs on one server do not block each other. Databases
left behind by a killed run are dropped by the next run once they are older
than ORPHAN_DATABASE_MAX_AGE. Migrations are applied once per test session
via Alembic; tables are truncated between tests.
"""

import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool

from control_plane.config import Settings
from control_plane.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "CP_TEST_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost:5434/control_plane_test",
)

BOOTSTRAP_TOKEN = "test-bootstrap-token"

# A run database older than this belongs to a run that died without its
# session teardown; no healthy run lives that long (pytest-timeout caps tests).
ORPHAN_DATABASE_MAX_AGE = 6 * 3600

_PG_IDENTIFIER_MAX = 63

_TRUNCATE_SQL = text(
    "TRUNCATE outbox, events, event_archive, event_consumer_cursors, event_journal_floor, "
    "idempotency_keys, observation_dedup_keys, "
    "skill_invocations, "
    "approval_outcome_actions, "
    "run_control_messages, run_child_results, run_child_handles, approvals, "
    "task_context_packs, task_comment_revisions, task_comments, artifacts, "
    "run_manifest_ephemerals, run_harness_manifests, "
    "runs, task_relations, task_requirements, "
    "principal_roles, principal_capabilities, principal_skills, workspace_members, "
    "roles, capabilities, skills, "
    "external_references, project_config_revisions, project_profiles, project_templates, "
    "workspaces, workspace_types, "
    "task_claims, tasks, task_counters, "
    "sessions, delegations, api_keys, iam_principal_bindings, principals, tenants CASCADE"
)


def run_database_pattern(base_name: str) -> re.Pattern[str]:
    """Names of per-run databases derived from ``base_name``."""
    return re.compile(rf"{re.escape(base_name)}_(\d{{10}})_[0-9a-f]{{8}}")


def _drop_orphan_databases(admin: Engine, base_name: str) -> None:
    pattern = run_database_pattern(base_name)
    deadline = time.time() - ORPHAN_DATABASE_MAX_AGE
    with admin.connect() as conn:
        names = conn.execute(text("SELECT datname FROM pg_database")).scalars().all()
        for name in names:
            match = pattern.fullmatch(name)
            if match and int(match.group(1)) < deadline:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def run_database_url() -> Iterator[URL]:
    """A fresh database for this pytest process, dropped when the session ends."""
    base_url = make_url(TEST_DATABASE_URL)
    base_name = base_url.database or ""
    run_name = f"{base_name}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    if len(run_name) > _PG_IDENTIFIER_MAX:  # pragma: no cover - environment guard
        pytest.exit(f"Test database name {base_name!r} is too long for a per-run suffix")
    admin = create_engine(base_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect():
            pass
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.exit(
            f"Test PostgreSQL is not reachable at {base_url!r}: {exc}\n"
            "Start it with: docker compose --profile test up -d db-test",
            returncode=3,
        )
    _drop_orphan_databases(admin, base_name)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{run_name}"'))
    try:
        yield base_url.set(database=run_name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{run_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def sync_engine(run_database_url: URL) -> Iterator[Engine]:
    engine = create_engine(run_database_url, poolclass=None)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated_database(run_database_url: URL, sync_engine: Engine) -> str:
    url = run_database_url.render_as_string(hide_password=False)
    # Alembic (here and in the migration tests) reads the URL from the environment.
    os.environ["CP_DATABASE_URL"] = url
    config = AlembicConfig("alembic.ini")
    alembic_command.upgrade(config, "head")
    return url


@pytest.fixture
def clean_database(migrated_database: str, sync_engine: Engine) -> None:
    """Truncate all tables; made autouse by the DB-backed test packages.

    ``events`` is protected by append-only triggers (including TRUNCATE), so
    the cleanup runs with triggers disabled via session_replication_role —
    the test role is the database superuser.
    """
    with sync_engine.begin() as conn:
        conn.execute(text("SET session_replication_role = replica"))
        conn.execute(_TRUNCATE_SQL)
        conn.execute(text("SET session_replication_role = DEFAULT"))


@pytest.fixture
def settings(migrated_database: str) -> Settings:
    return Settings(
        database_url=migrated_database,
        bootstrap_token=BOOTSTRAP_TOKEN,
        log_level="WARNING",
        session_ttl_seconds=60,
        claim_ttl_seconds=60,
        session_ttl_min_seconds=1,
        claim_ttl_min_seconds=1,
        ws_poll_interval_seconds=0.5,
        worker_poll_interval_seconds=0.05,
        idempotency_wait_timeout_seconds=5.0,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
