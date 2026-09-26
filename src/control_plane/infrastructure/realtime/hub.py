"""Realtime hub: LISTEN on PostgreSQL NOTIFY and wake WebSocket subscribers.

The hub is a wake-up signal only — never a source of truth. Subscribers
always read events from the ``events`` table by sequence; if a NOTIFY is
lost (or the LISTEN connection drops), subscribers still poll on a timer.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections import defaultdict

import psycopg

from control_plane.application.events import NOTIFY_CHANNEL

logger = logging.getLogger(__name__)


def _listen_dsn(database_url: str) -> str:
    """SQLAlchemy URL -> plain libpq DSN for the dedicated LISTEN connection."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class RealtimeHub:
    def __init__(self, database_url: str) -> None:
        self._dsn = _listen_dsn(database_url)
        self._waiters: defaultdict[uuid.UUID, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._listen_loop(), name="realtime-listen")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _listen_loop(self) -> None:
        backoff = 0.5
        while not self._stopping:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    logger.info("realtime hub listening", extra={"channel": NOTIFY_CHANNEL})
                    backoff = 0.5
                    async for notify in conn.notifies():
                        self._handle_notify(notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("realtime LISTEN connection lost; reconnecting", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _handle_notify(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            tenant_id = uuid.UUID(data["tenantId"])
        except (ValueError, KeyError, TypeError):
            logger.warning("malformed NOTIFY payload ignored")
            return
        self.wake(tenant_id)

    def wake(self, tenant_id: uuid.UUID) -> None:
        for waiter in self._waiters.get(tenant_id, ()):
            waiter.set()

    async def wait(self, tenant_id: uuid.UUID, timeout_seconds: float) -> None:
        """Wait until new events *may* exist for the tenant, or the timeout passes."""
        waiter = asyncio.Event()
        self._waiters[tenant_id].add(waiter)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter.wait(), timeout_seconds)
        finally:
            self._waiters[tenant_id].discard(waiter)
            if not self._waiters[tenant_id]:
                del self._waiters[tenant_id]
