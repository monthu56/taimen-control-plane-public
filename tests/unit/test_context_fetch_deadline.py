"""The tenant-only fallback of a memory read shares one deadline (CP-ADR-0059 p.4)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from control_plane.application.queries.context import fetch_memory
from control_plane.config import Settings
from control_plane.infrastructure.context_provider import ContextProviderError


class _SlowRejectThenHang:
    """Rejects the multi-namespace read late, then never answers the retry."""

    def __init__(self, reject_after: float) -> None:
        self.reject_after = reject_after
        self.calls: list[list[str]] = []
        self.requests: list[dict[str, Any]] = []

    async def build_context(
        self,
        *,
        namespace: str,
        request: dict[str, Any],
        trace_run_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(list(namespaces or [namespace]))
        self.requests.append(request)
        if len(self.calls) == 1:
            await asyncio.sleep(self.reject_after)
            raise ContextProviderError("extra field", retryable=False, status=422)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


def _memory_call() -> dict[str, Any]:
    return {
        "namespace": "tenant:t",
        "namespaces": ["tenant:t", "tenant:t:ws:w"],
        "request": {"query": "q", "allowedScopes": ["workspace:w"]},
    }


async def test_fallback_gets_the_remainder_of_one_deadline() -> None:
    # Budget = context_timeout_seconds + 1 s = 1 s. The rejection eats 0.7 s;
    # a fresh timeout for the retry would stretch the read to ~1.7 s.
    settings = Settings(database_url="postgresql+psycopg://x/y", context_timeout_seconds=0.0)
    provider = _SlowRejectThenHang(reject_after=0.7)
    response: dict[str, Any] = {"warnings": [], "memory": None, "memoryStatus": None}

    started = time.monotonic()
    await fetch_memory(response, _memory_call(), provider, settings)
    elapsed = time.monotonic() - started

    assert provider.calls == [["tenant:t", "tenant:t:ws:w"], ["tenant:t"]]
    # The retry drops the workspace namespace, not the narrowing.
    assert provider.requests[1]["allowedScopes"] == ["workspace:w"]
    assert response["memoryStatus"] == "timeout"
    assert elapsed < 1.4
