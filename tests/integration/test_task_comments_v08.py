"""Work item comments and the audit of their edits (ADR-0050, TASK-000028).

Four guarantees are under test here.

*Authorship is not a claim.* The author comes from the credential, so a body
that tries to name someone else is refused by the contract, and an agent's
reply is distinguishable from a person's in the stored row.

*An edit does not erase.* The superseded text lands in an append-only table
before the new text does, only the author may edit at all, and the history
survives an attempt to rewrite it directly in the database.

*The thread reads forward and stays honest.* Pages are oldest-first, a comment
written mid-pagination arrives on a later page rather than shifting the ones
already read, and a cursor minted by another ordering is refused instead of
paginating something else.

*Nothing sensitive leaks.* Credential-shaped text is refused while talking
*about* credentials is not, the journal carries references and never the body,
and no query crosses a tenant boundary.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)


async def add_comment(
    client: httpx.AsyncClient, key: str, task_ref: str, body: str, **extra: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tasks/{task_ref}/comments",
        json={"body": body, **extra},
        headers=auth(key),
    )


async def edit_comment(
    client: httpx.AsyncClient,
    key: str,
    task_ref: str,
    comment_id: str,
    body: str,
    *,
    version: int | None,
) -> httpx.Response:
    headers = auth(key)
    if version is not None:
        headers["If-Match"] = f'"comment-{version}"'
    return await client.patch(
        f"/api/v1/tasks/{task_ref}/comments/{comment_id}",
        json={"body": body},
        headers=headers,
    )


def event_payloads(sync_engine: Engine, event_type: str) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM events WHERE event_type = :t ORDER BY sequence"),
            {"t": event_type},
        ).all()
    return [row[0] for row in rows]


# --- authorship ---------------------------------------------------------------


async def test_author_comes_from_the_credential_not_from_the_body(
    client: httpx.AsyncClient,
) -> None:
    """Two principals, one thread: the rows say who actually spoke.

    A comment whose author could be asserted in the body would make the whole
    thread worthless as a record of who decided what.
    """
    bootstrap = await do_bootstrap(client)
    admin_key = bootstrap["apiKey"]["key"]
    human_id = bootstrap["adminPrincipal"]["id"]
    agent, agent_key = await create_agent_with_key(client, admin_key, name="runner")
    task = await create_task(client, admin_key, title="Shared work")

    from_human = await add_comment(client, admin_key, task["publicId"], "Starting on this.")
    assert from_human.status_code == 201, from_human.text
    assert from_human.json()["authorPrincipalId"] == human_id
    assert from_human.json()["version"] == 1
    assert from_human.json()["editedAt"] is None

    from_agent = await add_comment(client, agent_key, task["publicId"], "Picked it up.")
    assert from_agent.status_code == 201, from_agent.text
    assert from_agent.json()["authorPrincipalId"] == agent["id"]

    # Naming an author in the body is not a way to sign someone else's name:
    # the field does not exist in the contract, so the request never arrives.
    forged = await client.post(
        f"/api/v1/tasks/{task['publicId']}/comments",
        json={"body": "Not mine", "authorPrincipalId": agent["id"]},
        headers=auth(admin_key),
    )
    assert forged.status_code == 400, forged.text
    assert forged.json()["error"]["code"] == "invalid_request"


async def test_every_comment_names_its_author_in_words(client: httpx.AsyncClient) -> None:
    """``author {kind, displayName}`` on every read and write (amendment of 2026-09-30).

    A reader holding only ``tasks.read`` tells the owner from an agent without
    ``principals.read``; an idempotent replay answers the same author.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key, name="runner")
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["tasks.read"]
    )
    task = await create_task(client, admin_key, title="Who said it")
    ref = task["publicId"]
    owner = {"kind": "human", "displayName": "Admin"}
    runner = {"kind": "agent", "displayName": "runner"}

    headers = {**auth(admin_key), "Idempotency-Key": "comment-author-1"}
    first = await client.post(
        f"/api/v1/tasks/{ref}/comments", json={"body": "Owner speaking."}, headers=headers
    )
    assert first.status_code == 201, first.text
    assert first.json()["author"] == owner
    replay = await client.post(
        f"/api/v1/tasks/{ref}/comments", json={"body": "Owner speaking."}, headers=headers
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()

    from_agent = await add_comment(client, agent_key, ref, "Agent speaking.")
    assert from_agent.json()["author"] == runner
    assert from_agent.json()["authorPrincipalId"] == agent["id"]

    edited = await edit_comment(
        client, agent_key, ref, from_agent.json()["id"], "Agent, corrected.", version=1
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["author"] == runner

    listed = await client.get(f"/api/v1/tasks/{ref}/comments", headers=auth(reader_key))
    assert listed.status_code == 200, listed.text
    assert [(c["body"], c["author"]) for c in listed.json()["items"]] == [
        ("Owner speaking.", owner),
        ("Agent, corrected.", runner),
    ]
    one = await client.get(
        f"/api/v1/tasks/{ref}/comments/{first.json()['id']}", headers=auth(reader_key)
    )
    assert one.status_code == 200, one.text
    assert one.json()["author"] == owner
    # The reader still cannot read the principal itself: only the words travel.
    principal = await client.get(f"/api/v1/principals/{agent['id']}", headers=auth(reader_key))
    assert principal.status_code == 403, principal.text


async def test_cores_comment_names_a_service(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Core's own principal is a ``service``: runners tell its text from a person's."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Verified")
    with sync_engine.begin() as conn:
        core_id = conn.execute(
            text(
                "INSERT INTO principals"
                " (id, tenant_id, kind, display_name, status, metadata, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :tenant, 'service', 'Control Plane', 'active',"
                ' \'{"system": "control-plane-core"}\', now(), now()) RETURNING id'
            ),
            {"tenant": task["tenantId"]},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO task_comments (id, tenant_id, task_id, author_principal_id, body,"
                " version, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :tenant, :task, :author,"
                " 'Verification attempt #1 failed', 1, now(), now())"
            ),
            {"tenant": task["tenantId"], "task": task["id"], "author": core_id},
        )

    listed = await client.get(f"/api/v1/tasks/{task['publicId']}/comments", headers=auth(admin_key))
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["author"] == {"kind": "service", "displayName": "Control Plane"}
    assert item["authorPrincipalId"] == str(core_id)


async def test_reading_a_thread_requires_read_and_writing_requires_write(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Guarded")
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["tasks.read"]
    )

    denied = await add_comment(client, reader_key, task["publicId"], "May I?")
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "permission_denied"

    listed = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments", headers=auth(reader_key)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []


# --- editing and its audit ----------------------------------------------------


async def test_edit_keeps_the_previous_version_as_a_revision(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """What was said stays readable after it is corrected."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Correctable")

    created = (await add_comment(client, admin_key, task["publicId"], "Deploy on Tuesday")).json()

    edited = await edit_comment(
        client, admin_key, task["publicId"], created["id"], "Deploy on Thursday", version=1
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["body"] == "Deploy on Thursday"
    assert body["version"] == 2
    assert body["editedAt"] is not None

    revisions = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments/{created['id']}/revisions",
        headers=auth(admin_key),
    )
    assert revisions.status_code == 200, revisions.text
    items = revisions.json()["items"]
    assert len(items) == 1
    assert items[0]["version"] == 1
    assert items[0]["body"] == "Deploy on Tuesday"
    assert items[0]["authorPrincipalId"] == created["authorPrincipalId"]

    assert [p["version"] for p in event_payloads(sync_engine, "task.comment_edited")] == [2]


async def test_only_the_author_may_edit_even_with_admin(client: httpx.AsyncClient) -> None:
    """Rewriting someone else's words under their name is not a permission.

    The admin here can create principals and keys; it still cannot make the
    record say that the agent said something it did not.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Attributed")
    _, agent_key = await create_agent_with_key(client, admin_key, name="runner")

    theirs = (await add_comment(client, agent_key, task["publicId"], "Blocked on infra")).json()

    hijack = await edit_comment(
        client, admin_key, task["publicId"], theirs["id"], "Not blocked at all", version=1
    )
    assert hijack.status_code == 403, hijack.text
    assert hijack.json()["error"]["code"] == "not_comment_author"

    unchanged = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments/{theirs['id']}", headers=auth(admin_key)
    )
    assert unchanged.json()["body"] == "Blocked on infra"
    assert unchanged.json()["version"] == 1


async def test_edit_requires_if_match_and_refuses_a_stale_one(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Concurrent")
    created = (await add_comment(client, admin_key, task["publicId"], "First take")).json()

    missing = await edit_comment(
        client, admin_key, task["publicId"], created["id"], "Second take", version=None
    )
    assert missing.status_code == 428, missing.text
    assert missing.json()["error"]["code"] == "if_match_required"

    ok = await edit_comment(
        client, admin_key, task["publicId"], created["id"], "Second take", version=1
    )
    assert ok.status_code == 200, ok.text

    # The version the caller read is gone; the second writer must re-read.
    stale = await edit_comment(
        client, admin_key, task["publicId"], created["id"], "Third take", version=1
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "version_conflict"


async def test_a_no_op_edit_writes_no_history(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A retry that changes nothing must not manufacture an edit."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Idempotent")
    created = (await add_comment(client, admin_key, task["publicId"], "Same text")).json()

    repeated = await edit_comment(
        client, admin_key, task["publicId"], created["id"], "  Same text  ", version=1
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["version"] == 1
    assert repeated.json()["editedAt"] is None

    revisions = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments/{created['id']}/revisions",
        headers=auth(admin_key),
    )
    assert revisions.json()["items"] == []
    assert event_payloads(sync_engine, "task.comment_edited") == []


async def test_revision_history_cannot_be_rewritten_in_the_database(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The audit is append-only at the storage level, not by convention."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Auditable")
    created = (await add_comment(client, admin_key, task["publicId"], "Original wording")).json()
    await edit_comment(
        client, admin_key, task["publicId"], created["id"], "Revised wording", version=1
    )

    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(text("UPDATE task_comment_revisions SET body = 'rewritten'"))

    with pytest.raises(DBAPIError, match="append-only"), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM task_comment_revisions"))


# --- the thread ---------------------------------------------------------------


async def test_thread_reads_forward_and_survives_a_concurrent_insert(
    client: httpx.AsyncClient,
) -> None:
    """Pagination delivers every comment exactly once while people keep talking."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Busy thread")
    for index in range(4):
        posted = await add_comment(client, admin_key, task["publicId"], f"reply {index}")
        assert posted.status_code == 201, posted.text

    first = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments?limit=2", headers=auth(admin_key)
    )
    assert first.status_code == 200, first.text
    page_one = first.json()
    assert [c["body"] for c in page_one["items"]] == ["reply 0", "reply 1"]
    assert page_one["nextCursor"] is not None

    # Someone replies between the two page requests.
    await add_comment(client, admin_key, task["publicId"], "reply 4")

    second = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments",
        params={"limit": 2, "cursor": page_one["nextCursor"]},
        headers=auth(admin_key),
    )
    assert [c["body"] for c in second.json()["items"]] == ["reply 2", "reply 3"]

    third = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments",
        params={"limit": 2, "cursor": second.json()["nextCursor"]},
        headers=auth(admin_key),
    )
    # The late arrival is delivered, and nothing already read was repeated.
    assert [c["body"] for c in third.json()["items"]] == ["reply 4"]
    assert third.json()["nextCursor"] is None


async def test_a_cursor_from_another_ordering_is_refused(client: httpx.AsyncClient) -> None:
    """A newest-first cursor must not silently paginate a forward-read thread."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Cursor mixing")
    await add_comment(client, admin_key, task["publicId"], "hello")

    artifacts = await client.post(
        "/api/v1/artifacts",
        json={"type": "note", "name": "n1", "task": task["publicId"]},
        headers=auth(admin_key),
    )
    assert artifacts.status_code == 201, artifacts.text
    page = await client.get("/api/v1/artifacts?limit=1", headers=auth(admin_key))
    foreign_cursor = page.json()["nextCursor"]
    if foreign_cursor is None:
        await client.post(
            "/api/v1/artifacts",
            json={"type": "note", "name": "n2", "task": task["publicId"]},
            headers=auth(admin_key),
        )
        page = await client.get("/api/v1/artifacts?limit=1", headers=auth(admin_key))
        foreign_cursor = page.json()["nextCursor"]
    assert foreign_cursor is not None

    mixed = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments",
        params={"cursor": foreign_cursor},
        headers=auth(admin_key),
    )
    assert mixed.status_code == 422, mixed.text
    assert mixed.json()["error"]["code"] == "invalid_cursor"


async def test_a_terminal_task_still_accepts_a_retro_note(client: httpx.AsyncClient) -> None:
    """Closing work does not close its discussion."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Finished")
    session = await open_session(client, admin_key)
    claimed = await client.post(
        f"/api/v1/tasks/{task['publicId']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(admin_key),
    )
    assert claimed.status_code == 200, claimed.text
    current = await client.get(f"/api/v1/tasks/{task['publicId']}", headers=auth(admin_key))
    completed = await client.post(
        f"/api/v1/tasks/{task['publicId']}:complete",
        json={
            "claimId": claimed.json()["id"],
            "fencingToken": claimed.json()["fencingToken"],
        },
        headers={**auth(admin_key), "If-Match": f'"task-{current.json()["version"]}"'},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["systemStatusCategory"] == "terminal_success"

    late = await add_comment(client, admin_key, task["publicId"], "Retro: the rollback was manual.")
    assert late.status_code == 201, late.text


# --- provenance ---------------------------------------------------------------


async def test_an_attached_run_or_artifact_must_belong_to_the_same_task(
    client: httpx.AsyncClient,
) -> None:
    """A link that reads as provenance must not point at unrelated work."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    commented = await create_task(client, admin_key, title="Commented")
    other = await create_task(client, admin_key, title="Other work")

    session = await open_session(client, admin_key)
    claim = await client.post(
        f"/api/v1/tasks/{other['publicId']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(admin_key),
    )
    assert claim.status_code == 200, claim.text
    run = await client.post(
        f"/api/v1/tasks/{other['publicId']}:start-run",
        json={
            "claimId": claim.json()["id"],
            "fencingToken": claim.json()["fencingToken"],
        },
        headers=auth(admin_key),
    )
    assert run.status_code == 201, run.text

    artifact = await client.post(
        "/api/v1/artifacts",
        json={"type": "note", "name": "elsewhere", "task": other["publicId"]},
        headers=auth(admin_key),
    )
    assert artifact.status_code == 201, artifact.text

    wrong_run = await add_comment(
        client, admin_key, commented["publicId"], "See the run", runId=run.json()["id"]
    )
    assert wrong_run.status_code == 422, wrong_run.text
    assert wrong_run.json()["error"]["code"] == "comment_mismatch"

    wrong_artifact = await add_comment(
        client,
        admin_key,
        commented["publicId"],
        "See the artifact",
        artifactId=artifact.json()["id"],
    )
    assert wrong_artifact.status_code == 422, wrong_artifact.text
    assert wrong_artifact.json()["error"]["code"] == "comment_mismatch"


# --- what must not be stored --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "here is the key sk-abcdefghijklmnopqrstuvwx",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "db password: hunter2hunter2hunter2",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
        "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
async def test_credential_shaped_text_is_refused(client: httpx.AsyncClient, body: str) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="No secrets")

    response = await add_comment(client, admin_key, task["publicId"], body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "secret_material_rejected"


async def test_talking_about_credentials_is_still_allowed(client: httpx.AsyncClient) -> None:
    """The guard refuses material, not vocabulary — otherwise it silences people."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Rotation")

    response = await add_comment(
        client,
        admin_key,
        task["publicId"],
        "Rotate the API key and the deploy token before Friday; the password policy is fine.",
    )
    assert response.status_code == 201, response.text


async def test_an_empty_or_oversized_body_is_refused(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Bounded")

    blank = await add_comment(client, admin_key, task["publicId"], "   \n  ")
    assert blank.status_code == 422, blank.text
    assert blank.json()["error"]["code"] == "invalid_comment_body"

    # Over the cap the contract itself refuses it, before the body is read at
    # all — the domain check behind it is exercised in the unit tests.
    huge = await add_comment(client, admin_key, task["publicId"], "x" * 10_001)
    assert huge.status_code == 400, huge.text
    assert huge.json()["error"]["code"] == "invalid_request"


async def test_the_journal_carries_references_and_never_the_body(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The event stream travels further than the row it describes."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Journalled")
    secret_ish = "The customer's home address is on the invoice"

    created = (await add_comment(client, admin_key, task["publicId"], secret_ish)).json()

    payloads = event_payloads(sync_engine, "task.comment_added")
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["commentId"] == created["id"]
    assert payload["authorPrincipalId"] == created["authorPrincipalId"]
    assert payload["bodyLength"] == len(secret_ish)
    assert "body" not in payload
    assert secret_ish not in str(payload)


# --- tenant isolation ---------------------------------------------------------


async def test_a_comment_never_crosses_a_tenant_boundary(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Neither the task in the path nor the comment id resolves for an outsider."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Private")
    created = (await add_comment(client, admin_key, task["publicId"], "Internal note")).json()

    _, other_key = make_tenant_directly(sync_engine, "other")

    by_task = await client.get(
        f"/api/v1/tasks/{task['publicId']}/comments", headers=auth(other_key)
    )
    assert by_task.status_code == 404, by_task.text

    by_id = await client.get(
        f"/api/v1/tasks/{task['id']}/comments/{created['id']}", headers=auth(other_key)
    )
    assert by_id.status_code == 404, by_id.text

    written = await add_comment(client, other_key, task["publicId"], "Hello from outside")
    assert written.status_code == 404, written.text


async def test_a_comment_is_addressed_through_its_own_task(client: httpx.AsyncClient) -> None:
    """The task in the path is part of a comment's identity, not decoration."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Owner")
    decoy = await create_task(client, admin_key, title="Decoy")
    created = (await add_comment(client, admin_key, task["publicId"], "Belongs here")).json()

    wrong_path = await client.get(
        f"/api/v1/tasks/{decoy['publicId']}/comments/{created['id']}", headers=auth(admin_key)
    )
    assert wrong_path.status_code == 404, wrong_path.text

    wrong_edit = await edit_comment(
        client, admin_key, decoy["publicId"], created["id"], "Moved", version=1
    )
    assert wrong_edit.status_code == 404, wrong_edit.text
