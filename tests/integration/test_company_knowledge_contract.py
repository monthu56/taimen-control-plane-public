"""The company-knowledge amendment of CP-ADR-0060 keeps what came before it.

The contract (K003) answered ``501`` until each step; every step now has its
own tests: the snapshot preview and ``expectedState`` (K008) are
``test_knowledge_preview``, documents (K009) ``test_knowledge_documents``,
tenant packs (K010) ``test_knowledge_tenant_packs``. What stays here: a plain
snapshot works as before.
"""

import httpx
from fastapi import FastAPI

from tests.helpers import (
    FakeKnowledge,
    auth,
    create_workspace,
    do_bootstrap,
)
from tests.helpers import (
    knowledge_snapshot as snapshot,
)


async def test_a_snapshot_without_expected_state_works_as_before(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    root = await create_workspace(client, admin_key, "root")
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        response = await client.post(
            "/api/v1/knowledge/snapshots",
            json={**snapshot(), "workspaceId": root["id"]},
            headers=auth(admin_key),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200, response.text
    [(kind, call)] = fake.calls
    assert kind == "reconcile"
    assert "expectedState" not in call["snapshot"]
