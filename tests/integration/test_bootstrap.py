import httpx

from tests.helpers import BOOTSTRAP_TOKEN, auth, do_bootstrap


async def test_bootstrap_requires_token(client: httpx.AsyncClient) -> None:
    payload = {"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "Admin"}

    response = await client.post("/api/v1/bootstrap", json=payload)
    assert response.status_code == 401

    response = await client.post("/api/v1/bootstrap", json=payload, headers=auth("wrong-token"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_bootstrap_creates_tenant_admin_and_working_key(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)

    assert body["tenant"]["slug"] == "acme"
    assert body["adminPrincipal"]["kind"] == "human"
    assert body["apiKey"]["permissions"] == ["admin"]
    full_key = body["apiKey"]["key"]
    assert full_key.startswith("cp_")

    # The returned key authenticates and carries admin permissions.
    response = await client.get("/api/v1/principals", headers=auth(full_key))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_bootstrap_is_one_time(client: httpx.AsyncClient) -> None:
    await do_bootstrap(client)
    response = await client.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "other", "tenantName": "Other", "adminDisplayName": "X"},
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_bootstrapped"


async def test_error_envelope_shape(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "??", "tenantName": "Acme", "adminDisplayName": "A"},
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "requestId"}
    assert error["code"] == "invalid_request"
    assert response.headers["x-request-id"] == error["requestId"]
