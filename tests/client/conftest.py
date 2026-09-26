import httpx
import pytest
from fastapi import FastAPI

from control_plane_client import ControlPlaneClient


@pytest.fixture(autouse=True)
def _clean(clean_database: None) -> None:
    """Every test in this package starts from an empty, migrated database."""


@pytest.fixture
def sdk(app: FastAPI):
    """Factory: an SDK client for a given API key, wired straight to the app."""
    created: list[ControlPlaneClient] = []

    def make(api_key: str) -> ControlPlaneClient:
        client = ControlPlaneClient(
            "http://testserver",
            api_key,
            transport=httpx.ASGITransport(app=app),
        )
        created.append(client)
        return client

    yield make
    # closed by tests via async context or explicitly; nothing to await here
