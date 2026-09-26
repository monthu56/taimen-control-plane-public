"""Contract tests against a REAL Memory Service over HTTP.

No mocks and no Python imports from the memory repo — only the wire.
Activated by env vars (skipped otherwise):

    CP_TEST_MEMORY_URL=http://127.0.0.1:8079
    CP_TEST_MEMORY_API_KEY=memtest-api-key-0123456789

A disposable instance:

    cd ../memory-service/deploy && docker compose -p cp-memtest \
        --env-file <env> up -d --build
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from control_plane.infrastructure.context_provider.http import HttpContextProvider

MEMORY_URL = os.environ.get("CP_TEST_MEMORY_URL")
MEMORY_KEY = os.environ.get("CP_TEST_MEMORY_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not MEMORY_URL, reason="CP_TEST_MEMORY_URL not set (real Memory Service required)"
)


@pytest.fixture
async def provider() -> AsyncIterator[HttpContextProvider]:
    assert MEMORY_URL is not None
    instance = HttpContextProvider(
        base_url=MEMORY_URL,
        api_key=MEMORY_KEY,
        timeout_seconds=5.0,
        ingest_timeout_seconds=15.0,
    )
    yield instance
    await instance.aclose()


@pytest.fixture
def namespace() -> str:
    """Fresh namespace per test — Memory namespaces are created implicitly."""
    return f"tenant:{uuid.uuid4()}"
