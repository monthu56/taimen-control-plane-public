import httpx

from control_plane.config import Settings
from control_plane.main import create_app
from tests.helpers import auth, do_bootstrap


async def test_health_live_and_ready(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}

    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["revision"]


async def test_ready_fails_without_database(settings: Settings) -> None:
    broken = settings.model_copy(
        update={"database_url": "postgresql+psycopg://nobody:x@localhost:59999/none"}
    )
    application = create_app(broken)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as bad_client:
            response = await bad_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_unreachable"


async def test_metrics_exposition(client: httpx.AsyncClient) -> None:
    await client.get("/health/live")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text


async def test_request_id_passthrough_and_generation(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "my-req-1"})
    assert response.headers["x-request-id"] == "my-req-1"

    response = await client.get("/health/live", headers={"X-Request-ID": "bad id \n"})
    assert response.headers["x-request-id"].startswith("req_")  # sanitized

    response = await client.get("/health/live")
    assert response.headers["x-request-id"].startswith("req_")


async def test_body_size_limit(client: httpx.AsyncClient) -> None:
    await do_bootstrap(client)
    huge = "x" * 2_000_000
    response = await client.post("/api/v1/bootstrap", json={"tenantSlug": huge, "tenantName": "x"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


async def test_openapi_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = spec["paths"]
    assert "/api/v1/tasks/{task_ref}:claim" in paths
    assert "/api/v1/bootstrap" in paths
    # The shared error envelope is part of the contract.
    assert "ErrorEnvelope" in spec["components"]["schemas"]


async def test_unknown_route_uses_error_envelope(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    response = await client.get("/api/v1/nowhere", headers=auth(body["apiKey"]["key"]))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
