"""Text search over the task list: ``GET /tasks?q=`` (CP-ADR-0049, amendment
TASK-000866).

*It is a filter like the others.* ``q`` narrows the statement before the
cursor predicate, so it combines with every filter, both orderings and the
cursor without skipping or repeating a row, and it never widens what the
caller may read — tenant and workspace visibility stay as they were.

*It matches what a person types.* Every whitespace-separated term must occur,
case-insensitively, as a substring of the title, the description or the public
id, in Russian as in English. LIKE wildcards in the input are literal text.

*It does not scan.* Each column has a pg_trgm GIN index and the predicate has
the shape those indexes serve.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from platform_auth import ObjectPage, PolicyDecision
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.application.queries.lists import (
    list_tasks as list_tasks_query,
)
from control_plane.application.queries.lists import (
    task_search_clause,
    task_search_terms,
)
from control_plane.infrastructure.db.models import Task
from tests.helpers import auth, create_task, create_workspace, do_bootstrap, make_tenant_directly


async def search(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/tasks", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def titles(client: httpx.AsyncClient, key: str, **params: Any) -> set[str]:
    return {t["title"] for t in (await search(client, key, **params))["items"]}


@pytest.fixture
async def admin_key(client: httpx.AsyncClient) -> str:
    return (await do_bootstrap(client))["apiKey"]["key"]


# --- what matches -------------------------------------------------------------


async def test_cyrillic_is_matched_regardless_of_case(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    await create_task(client, admin_key, title="Починить Логин в консоли")
    await create_task(client, admin_key, title="Обновить документацию")

    for q in ("логин", "ЛОГИН", "ЛоГиН", "консол"):
        assert await titles(client, admin_key, q=q) == {"Починить Логин в консоли"}, q


async def test_a_partial_word_matches_in_english(client: httpx.AsyncClient, admin_key: str) -> None:
    await create_task(client, admin_key, title="Refactoring the Scheduler")
    await create_task(client, admin_key, title="Unrelated")

    assert await titles(client, admin_key, q="FACTOR") == {"Refactoring the Scheduler"}
    assert await titles(client, admin_key, q="dul") == {"Refactoring the Scheduler"}


async def test_the_description_is_searched(client: httpx.AsyncClient, admin_key: str) -> None:
    await create_task(client, admin_key, title="A", description="Падает импорт счетов из банка")
    await create_task(client, admin_key, title="B", description="nothing here")

    assert await titles(client, admin_key, q="ИМПОРТ счет") == {"A"}


async def test_the_public_id_is_searched_in_any_case(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    first = await create_task(client, admin_key, title="first")
    await create_task(client, admin_key, title="second")

    public_id = first["publicId"]
    assert await titles(client, admin_key, q=public_id) == {"first"}
    assert await titles(client, admin_key, q=public_id.lower()) == {"first"}


async def test_every_term_must_occur_but_in_any_field(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    await create_task(client, admin_key, title="alpha release", description="beta notes")
    await create_task(client, admin_key, title="alpha only")

    assert await titles(client, admin_key, q="beta alpha") == {"alpha release"}
    assert await titles(client, admin_key, q="  alpha  ") == {"alpha release", "alpha only"}
    assert await titles(client, admin_key, q="alpha gamma") == set()


async def test_like_wildcards_are_literal_text(client: httpx.AsyncClient, admin_key: str) -> None:
    await create_task(client, admin_key, title="discount 50% off")
    await create_task(client, admin_key, title="snake_case name")
    await create_task(client, admin_key, title=r"path C:\temp")
    await create_task(client, admin_key, title="plain words")

    assert await titles(client, admin_key, q="%") == {"discount 50% off"}
    assert await titles(client, admin_key, q="_") == {"snake_case name"}
    assert await titles(client, admin_key, q="\\") == {r"path C:\temp"}


async def test_an_empty_query_is_no_filter(client: httpx.AsyncClient, admin_key: str) -> None:
    await create_task(client, admin_key, title="one")
    await create_task(client, admin_key, title="two")

    for q in ("", "   "):
        assert await titles(client, admin_key, q=q) == {"one", "two"}


async def test_an_oversized_query_is_refused(client: httpx.AsyncClient, admin_key: str) -> None:
    await create_task(client, admin_key, title="x" * 10)

    assert await titles(client, admin_key, q="x" * 200) == set()
    too_long = await client.get("/api/v1/tasks", params={"q": "x" * 201}, headers=auth(admin_key))
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "invalid_search"

    too_many = await client.get(
        "/api/v1/tasks", params={"q": " ".join("abcdefghijk")}, headers=auth(admin_key)
    )
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "invalid_search"


# --- together with filters, ordering and paging --------------------------------


async def test_filters_and_search_narrow_together(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    ws = await create_workspace(client, admin_key, "billing")
    await create_task(client, admin_key, title="счёт high", priority="high", workspaceId=ws["id"])
    await create_task(client, admin_key, title="счёт low", priority="low", workspaceId=ws["id"])
    await create_task(client, admin_key, title="счёт elsewhere", priority="high")
    await create_task(client, admin_key, title="other high", priority="high", workspaceId=ws["id"])

    assert await titles(client, admin_key, q="СЧЁТ", priority="high", workspaceId=ws["id"]) == {
        "счёт high"
    }
    assert await titles(client, admin_key, q="счёт", systemStatusCategory="active") == {
        "счёт high",
        "счёт low",
        "счёт elsewhere",
    }
    assert await titles(client, admin_key, q="счёт", status="no-such-status") == set()


async def test_search_under_the_due_date_ordering(
    client: httpx.AsyncClient, admin_key: str
) -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    await create_task(
        client, admin_key, title="deploy late", dueDate=(t0 + timedelta(days=3)).isoformat()
    )
    await create_task(client, admin_key, title="deploy soon", dueDate=t0.isoformat())
    await create_task(client, admin_key, title="deploy undated")
    await create_task(client, admin_key, title="other soon", dueDate=t0.isoformat())

    page = await search(client, admin_key, q="deploy", sort="dueDate")

    assert [t["title"] for t in page["items"]] == ["deploy soon", "deploy late", "deploy undated"]


@pytest.mark.parametrize("sort", [None, "dueDate"])
async def test_paging_a_search_covers_every_match_exactly_once(
    client: httpx.AsyncClient, admin_key: str, sort: str | None
) -> None:
    due = datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    matching = set()
    for i in range(7):
        # Interleave non-matching rows so a cursor landing on one would show.
        matching.add(
            (await create_task(client, admin_key, title=f"Миграция {i}", dueDate=due))["id"]
        )
        await create_task(client, admin_key, title=f"noise {i}", dueDate=due)

    seen: list[str] = []
    params: dict[str, Any] = {"q": "миграц", "limit": 3}
    if sort:
        params["sort"] = sort
    while True:
        page = await search(client, admin_key, **params)
        seen.extend(t["id"] for t in page["items"])
        if page["nextCursor"] is None:
            break
        params["cursor"] = page["nextCursor"]

    assert len(seen) == len(matching)
    assert set(seen) == matching


# --- visibility ----------------------------------------------------------------


async def test_search_does_not_cross_the_tenant_boundary(
    client: httpx.AsyncClient, admin_key: str, sync_engine: Engine
) -> None:
    ours = await create_task(client, admin_key, title="секретный проект")
    _, other_key = make_tenant_directly(sync_engine, "other")

    assert await titles(client, other_key, q="секретный") == set()
    assert await titles(client, other_key, q=ours["publicId"]) == set()


@dataclass
class ReadableWorkspaces:
    """A PDP that allows every check and lists only the given workspaces."""

    readable: set[str]

    async def check(
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):  # type: ignore[no-untyped-def]
        return PolicyDecision(
            allowed=True,
            reason_code="allowed",
            decision_id=str(uuid.uuid4()),
            policy_version="1",
            model_version="1",
            source="online",
            consistency_token=None,
            evaluated_at=datetime.now(UTC),
            action=action,
            resource=resource.key,
        )

    async def list_objects(self, ctx, action, resource_type, **kwargs):  # type: ignore[no-untyped-def]
        return ObjectPage(objects=sorted(self.readable), cursor=None, model_version="1")


@pytest.fixture
def restore_authorizer() -> Any:
    yield
    configure_authorizer(Authorizer(None, "local"))


async def test_search_in_policy_mode_stays_within_readable_workspaces(
    client: httpx.AsyncClient, app: Any, restore_authorizer: None
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    ws_a = await create_workspace(client, admin_key, "alpha")
    ws_b = await create_workspace(client, admin_key, "beta")
    await create_task(client, admin_key, title="отчёт A", workspaceId=ws_a["id"])
    await create_task(client, admin_key, title="отчёт B", workspaceId=ws_b["id"])

    configure_authorizer(Authorizer(ReadableWorkspaces(readable={ws_a["id"]}), "policy"))
    # A reader who neither created, owns nor is assigned any of the tasks.
    ctx = AuthContext(
        tenant_id=uuid.UUID(boot["tenant"]["id"]),
        principal_id=uuid.uuid4(),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )
    async with app.state.session_factory() as session:
        page = await list_tasks_query(session, ctx, q="ОТЧЁТ")

    assert [t.title for t in page.items] == ["отчёт A"]


# --- the index ------------------------------------------------------------------


def _explain(sync_engine: Engine, stmt: Any, *, seqscan: bool = False) -> str:
    # Named paramstyle: the literals are rendered without %-escaping, and the
    # driver escapes the text exactly once.
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(paramstyle="named"),
            compile_kwargs={"literal_binds": True},
        )
    )
    with sync_engine.connect() as conn:
        if not seqscan:
            conn.execute(text("SET enable_seqscan = off"))
        return "\n".join(row[0] for row in conn.execute(text(f"EXPLAIN {sql}")))


def test_the_search_predicate_is_served_by_the_trigram_indexes(
    migrated_database: str, sync_engine: Engine
) -> None:
    """The exact predicate the list builds is answered from the three trigram
    indexes, a BitmapOr per term — every column and every term, which the
    populated-tenant test below does not pin. Sequential scans are off and the
    tenant filter is left out: on an empty table either would win."""
    stmt = select(Task.id).where(task_search_clause(task_search_terms("логин TASK-0001")))

    plan = _explain(sync_engine, stmt)

    assert "Seq Scan" not in plan, plan
    assert "BitmapOr" in plan, plan
    for index in ("ix_tasks_title_trgm", "ix_tasks_description_trgm", "ix_tasks_public_id_trgm"):
        assert index in plan, plan


async def test_a_selective_search_on_a_populated_tenant_picks_the_trigram_indexes(
    client: httpx.AsyncClient, admin_key: str, sync_engine: Engine
) -> None:
    """With no planner settings touched, the list's own statement — tenant,
    search, order, limit — reads a selective search from the trigram indexes
    rather than scanning the tenant's tasks. VACUUM stands in for autovacuum:
    GIN keeps fresh rows in its pending list and its statistics in the
    metapage, and only VACUUM moves either."""
    seed = await create_task(client, admin_key, title="seed", description="seed body")
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks SELECT (jsonb_populate_record(NULL::tasks, to_jsonb(t) "
                "|| jsonb_build_object('id', gen_random_uuid(), 'public_id', 'BULK-' || g, "
                "'title', md5(g::text), 'description', repeat(md5((-g)::text) || ' ', 8)))).* "
                "FROM tasks t, generate_series(1, 3000) g WHERE t.id = :id"
            ),
            {"id": seed["id"]},
        )
    with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE tasks"))

    # md5('42') starts with a1d0c6e83f02: one title matches, nothing else.
    for q in ("C6E83F02", "BULK-2042"):
        stmt = (
            select(Task.id)
            .where(
                Task.tenant_id == uuid.UUID(seed["tenantId"]),
                task_search_clause(task_search_terms(q)),
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(51)
        )
        plan = _explain(sync_engine, stmt, seqscan=True)

        assert "Seq Scan" not in plan, plan
        assert "BitmapOr" in plan, plan
        assert "ix_tasks_title_trgm" in plan, plan
