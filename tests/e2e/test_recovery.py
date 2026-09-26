"""Recovery: state survives an API/worker restart; expiry and outbox catch up."""

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.main import create_app
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    backdate_expiry,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)


async def test_state_survives_restart_and_sweeps_catch_up(
    settings: Settings, sync_engine: Engine
) -> None:
    # --- first API process lifetime ------------------------------------------
    app1 = create_app(settings)
    async with (
        app1.router.lifespan_context(app1),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://t") as client,
    ):
        body = await do_bootstrap(client)
        admin_key = body["apiKey"]["key"]
        _, agent_key = await create_agent_with_key(client, admin_key)
        session = await open_session(client, agent_key)
        task = await create_task(client, admin_key)
        claim = (
            await client.post(
                f"/api/v1/tasks/{task['id']}:claim",
                json={"sessionId": session["id"]},
                headers=auth(agent_key),
            )
        ).json()
    # App 1 is fully shut down here (engine disposed, hub stopped).

    # --- second API process lifetime -----------------------------------------
    app2 = create_app(settings)
    async with (
        app2.router.lifespan_context(app2),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t") as client,
    ):
        # Active session and claim are still visible after restart.
        session_now = (
            await client.get(f"/api/v1/sessions/{session['id']}", headers=auth(agent_key))
        ).json()
        claim_now = (
            await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(agent_key))
        ).json()
        assert session_now["status"] == "active"
        assert claim_now["status"] == "active"

        # Leases expire while "everything was down".
        backdate_expiry(sync_engine, "sessions", session["id"])
        backdate_expiry(sync_engine, "task_claims", claim["id"])

        # A fresh worker (as after restart) picks everything up:
        # stale sweeps AND the outbox backlog accumulated before the crash.
        with sync_engine.connect() as conn:
            backlog = conn.execute(
                text("SELECT count(*) FROM outbox WHERE delivered_at IS NULL")
            ).scalar()
        assert backlog and backlog > 0

        worker = Worker(settings)
        try:
            stats = await worker.run_once()
            assert stats["sessions_expired"] == 1
            # The claim is released by the session sweep (its session died),
            # so the dedicated claim sweep finds nothing left to reap.
            assert stats["claims_expired"] == 0
            # The pre-crash backlog is delivered in the first cycle; the two
            # expiry events recorded during it go out on the next cycle.
            assert stats["outbox_delivered"] == backlog
            second = await worker.run_once()
            assert second["outbox_delivered"] == 2
        finally:
            await worker.engine.dispose()

        session_after = (
            await client.get(f"/api/v1/sessions/{session['id']}", headers=auth(agent_key))
        ).json()
        claim_after = (
            await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(agent_key))
        ).json()
        task_after = (
            await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))
        ).json()
        assert session_after["status"] == "stale"
        assert claim_after["status"] == "released"
        assert claim_after["releaseReason"] == "session_expired"
        assert task_after["activeClaimId"] is None

        with sync_engine.connect() as conn:
            remaining = conn.execute(
                text("SELECT count(*) FROM outbox WHERE delivered_at IS NULL")
            ).scalar()
        assert remaining == 0
