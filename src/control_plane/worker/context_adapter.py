"""Context Adapter: replays the event journal into the Context Memory Engine.

A separate background process (``python -m control_plane.worker.context_adapter``)
with its own lifecycle: it never participates in request transactions, and
its crash or the Memory Service being down cannot affect API/worker
correctness — events simply accumulate and the durable cursors stay behind.

Delivery contract: **at-least-once, lossless, per tenant**.

    for each due tenant (round-robin, least-recently-served first):
        read events after that tenant's cursor (safe (tx_id, sequence) order,
          stable horizon)
        → whitelist/translate (application/context/mapping.py)
        → POST one observation batch to the tenant's namespace
        → provider confirms everything
        → THEN advance that tenant's durable cursor

A crash between provider confirm and cursor commit re-delivers the batch;
the Memory Service deduplicates on stable observation identity. A permanent
provider rejection (poison) does NOT advance the cursor: **only the failing
tenant's row parks** with escalating backoff and visible diagnostics, while
every other tenant keeps flowing (ADR-0036). Losing an observation silently
would break memory rebuild, so nothing here can skip an event — un-parking is
an explicit operator action that retries the same position (ADR-0037).

Singleton: one consumer process, enforced with a session-level PostgreSQL
advisory lock — a second replica waits, it does not double-consume. Per-tenant
isolation here is isolation of *failures*, not parallelism: adding a scheduler
was explicitly out of scope.
"""

import asyncio
import contextlib
import logging
import signal
import uuid
from datetime import timedelta
from typing import Any
from typing import cast as type_cast

from sqlalchemy import BigInteger, ColumnElement, Text, cast, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from control_plane.application.common import utcnow
from control_plane.application.context.mapping import map_event
from control_plane.application.event_cursor import EventPosition
from control_plane.application.queries.events import JournalEvent, fetch_events_after
from control_plane.config import Settings
from control_plane.infrastructure.context_provider import (
    ContextProvider,
    ContextProviderError,
    build_context_provider,
    tenant_namespace,
)
from control_plane.infrastructure.db.engine import build_engine, build_session_factory, transaction
from control_plane.infrastructure.db.models import EventConsumerCursor

logger = logging.getLogger(__name__)

CONSUMER_NAME = "context-adapter"
# Fixed advisory-lock key for the singleton (arbitrary but stable constant).
_ADVISORY_LOCK_KEY = 0x00C0047E


def _stable_horizon() -> ColumnElement[int]:
    return cast(cast(func.pg_snapshot_xmin(func.pg_current_snapshot()), Text), BigInteger)


class ContextAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
        provider: ContextProvider | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine or build_engine(settings)
        self.session_factory = build_session_factory(self.engine)
        self.provider = provider if provider is not None else build_context_provider(settings)
        self._stop = asyncio.Event()
        self.consecutive_failures = 0

    def request_stop(self) -> None:
        self._stop.set()

    # -- cursor ----------------------------------------------------------------

    async def ensure_cursor_rows(self) -> None:
        """One cursor row per tenant; new tenants correctly start at the origin."""
        async with transaction(self.session_factory) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO event_consumer_cursors
                        (name, tenant_id, tx_id, sequence, updated_at, metadata, failure_count)
                    SELECT :name, t.id, 0, 0, now(), '{}'::jsonb, 0 FROM tenants t
                    ON CONFLICT (name, tenant_id) DO NOTHING
                    """
                ),
                {"name": CONSUMER_NAME},
            )

    async def load_cursor(self, tenant_id: uuid.UUID) -> EventPosition:
        async with self.session_factory() as session:
            row = await session.get(EventConsumerCursor, (CONSUMER_NAME, tenant_id))
            if row is None:
                return EventPosition(0, 0)
            return EventPosition(tx_id=row.tx_id, sequence=row.sequence)

    async def due_tenants(self) -> list[uuid.UUID]:
        """Tenants with pending work whose backoff (if any) has elapsed.

        Ordered least-recently-served first: one busy tenant cannot starve the
        others, and a parked tenant is skipped until its next attempt is due.
        """
        async with self.session_factory() as session:
            rows = await session.execute(
                text(
                    """
                    SELECT c.tenant_id
                      FROM event_consumer_cursors c
                     WHERE c.name = :name
                       AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= now())
                       AND EXISTS (
                           SELECT 1 FROM events e
                            WHERE e.tenant_id = c.tenant_id
                              AND (e.tx_id, e.sequence) > (c.tx_id, c.sequence)
                              AND e.tx_id < pg_snapshot_xmin(pg_current_snapshot())::text::bigint
                       )
                     ORDER BY c.updated_at ASC
                     LIMIT :limit
                    """
                ),
                {"name": CONSUMER_NAME, "limit": self.settings.context_max_tenants_per_cycle},
            )
            return [row[0] for row in rows]

    async def _locked_row(self, session: Any, tenant_id: uuid.UUID) -> EventConsumerCursor:
        row = await session.get(
            EventConsumerCursor, (CONSUMER_NAME, tenant_id), with_for_update=True
        )
        if row is None:
            # Concurrent-create safe (a second instance racing through an
            # advisory-lock handover must not crash on the unique key).
            await session.execute(
                pg_insert(EventConsumerCursor)
                .values(name=CONSUMER_NAME, tenant_id=tenant_id, tx_id=0, sequence=0)
                .on_conflict_do_nothing(index_elements=["name", "tenant_id"])
            )
            row = await session.get(
                EventConsumerCursor, (CONSUMER_NAME, tenant_id), with_for_update=True
            )
            assert row is not None
        return type_cast(EventConsumerCursor, row)

    async def commit_cursor(
        self,
        tenant_id: uuid.UUID,
        position: EventPosition,
        *,
        expected_from: EventPosition | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        async with transaction(self.session_factory) as session:
            row = await self._locked_row(session, tenant_id)
            merged = dict(row.metadata_ or {})
            if expected_from is not None and (row.tx_id, row.sequence) != (
                expected_from.tx_id,
                expected_from.sequence,
            ):
                # An operator rebuilt (moved the cursor back) while this batch
                # was in flight. Committing would silently undo the rebuild and
                # the requested replay would never happen (ADR-0037).
                merged["stale_commits_total"] = int(merged.get("stale_commits_total", 0)) + 1
                row.metadata_ = merged
                return
            # Monotonic guard: a stale instance finishing an old batch after a
            # lock handover must never move the cursor BACKWARDS — nor
            # double-count stats or clear diagnostics the live instance owns.
            if (position.tx_id, position.sequence) > (row.tx_id, row.sequence):
                row.tx_id = position.tx_id
                row.sequence = position.sequence
                row.updated_at = utcnow()
                row.parked_at = None
                row.parked_reason = None
                row.parked_event_id = None
                row.failure_count = 0
                row.next_attempt_at = None
                if stats is not None:
                    for key, value in stats.items():
                        if key.endswith("_total"):
                            try:
                                merged[key] = int(merged.get(key, 0)) + int(value)
                            except (TypeError, ValueError):
                                merged[key] = int(value)
                        else:
                            merged[key] = value
            else:
                merged["stale_commits_total"] = int(merged.get("stale_commits_total", 0)) + 1
            row.metadata_ = merged

    async def record_failure(
        self, tenant_id: uuid.UUID, message: str, *, event_id: uuid.UUID | None = None
    ) -> None:
        """Park ONE tenant with a visible diagnostic; never advance its cursor."""
        async with transaction(self.session_factory) as session:
            row = await self._locked_row(session, tenant_id)
            row.failure_count += 1
            row.parked_at = utcnow()
            row.parked_reason = message[:2000]
            row.parked_event_id = event_id
            backoff = min(
                self.settings.context_retry_backoff_base_seconds
                * (2 ** max(0, row.failure_count - 1)),
                self.settings.context_retry_backoff_max_seconds,
            )
            row.next_attempt_at = utcnow() + timedelta(seconds=backoff)
            row.updated_at = utcnow()
            merged = dict(row.metadata_ or {})
            merged["failures_total"] = int(merged.get("failures_total", 0)) + 1
            merged["last_error"] = message[:2000]
            merged["last_error_at"] = utcnow().isoformat()
            row.metadata_ = merged

    # -- one delivery cycle ----------------------------------------------------

    async def fetch_batch(self, tenant_id: uuid.UUID, after: EventPosition) -> list[JournalEvent]:
        """The next batch, spanning the cold archive exactly like ``/events``.

        Reading only the hot table would make a rebuild below the journal
        floor skip every archived event — a silent, permanent gap in the
        rebuilt memory (ADR-0038).
        """
        async with self.session_factory() as session:
            return await fetch_events_after(
                session,
                tenant_id=tenant_id,
                start=after,
                limit=self.settings.context_tenant_batch_size,
            )

    async def deliver_tenant(self, tenant_id: uuid.UUID) -> int:
        """Deliver one batch for one tenant; returns #events the cursor moved over."""
        if self.provider is None:  # pragma: no cover - guarded by main()
            return 0
        cursor = await self.load_cursor(tenant_id)
        events = await self.fetch_batch(tenant_id, cursor)
        if not events:
            return 0

        observations: list[dict[str, Any]] = []
        for event in events:
            observation = map_event(event)
            if observation is not None:
                observations.append(observation)

        delivered = 0
        duplicates = 0
        if observations:
            namespace = tenant_namespace(self.settings, tenant_id)
            # The batch spans many originating traces, so the request-level
            # header is the adapter's own; each observation carries its
            # event's traceRunId in `data` (ADR-0039).
            result = await self.provider.ingest_batch(
                namespace=namespace,
                observations=observations,
                trace_run_id=f"adapter_{uuid.uuid4().hex}",
            )
            if not result.fully_delivered:
                # Poison unit: do NOT advance past it. Park with diagnostics.
                raise ContextProviderError(
                    f"provider rejected {result.failed} observation(s) in {namespace}: "
                    f"{result.errors[:3]}",
                    retryable=False,
                )
            delivered = result.accepted
            duplicates = result.duplicates

        last = events[-1]
        await self.commit_cursor(
            tenant_id,
            EventPosition(tx_id=last.tx_id, sequence=last.sequence),
            expected_from=cursor,
            stats={
                "delivered_total": delivered,
                "duplicates_total": duplicates,
                "last_error": None,
                "last_delivery_at": utcnow().isoformat(),
            },
        )
        return len(events)

    async def deliver_once(self) -> int:
        """One cycle over the due tenants; returns the total events advanced."""
        if self.provider is None:  # pragma: no cover - guarded by main()
            return 0
        await self.ensure_cursor_rows()
        moved = 0
        failures = 0
        for tenant_id in await self.due_tenants():
            try:
                moved += await self.deliver_tenant(tenant_id)
            except ContextProviderError as exc:
                # One tenant's poison never stops the others: park this row and
                # keep going through the rest of the cycle.
                failures += 1
                poison = await self._first_undelivered_event_id(tenant_id)
                await self.record_failure(tenant_id, str(exc), event_id=poison)
                log = logger.warning if exc.retryable else logger.error
                log(
                    "context delivery parked for tenant",
                    extra={
                        "tenant": str(tenant_id),
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
            except Exception as exc:
                failures += 1
                await self.record_failure(tenant_id, f"{type(exc).__name__}: {exc}")
                logger.exception("context delivery failed", extra={"tenant": str(tenant_id)})
        self.consecutive_failures = failures
        return moved

    async def _first_undelivered_event_id(self, tenant_id: uuid.UUID) -> uuid.UUID | None:
        """The event the cursor is parked on — the operator's starting point."""
        cursor = await self.load_cursor(tenant_id)
        async with self.session_factory() as session:
            batch = await fetch_events_after(session, tenant_id=tenant_id, start=cursor, limit=1)
        return batch[0].id if batch else None

    # -- process loop ----------------------------------------------------------

    async def _acquire_singleton_lock(self) -> Any:
        """Session-level advisory lock on a dedicated connection.

        Held while the underlying session lives; a second adapter instance
        blocks here instead of double-consuming. The lock is best-effort
        singleton enforcement — the delivery loop re-verifies the connection
        each cycle and re-acquires on loss, and correctness never depends on
        it (at-least-once + provider dedup + the monotonic cursor guard).
        """
        connection = await self.engine.connect()
        while not self._stop.is_set():
            got = await connection.scalar(select(func.pg_try_advisory_lock(_ADVISORY_LOCK_KEY)))
            # Close the statement's implicit transaction: an idle-in-
            # transaction session can be killed by server timeouts, which
            # would silently release the session-level lock.
            await connection.commit()
            if got:
                return connection
            logger.info("another context-adapter holds the lock; waiting")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), self.settings.context_poll_interval_seconds * 5
                )
        await connection.close()
        return None

    async def _lock_alive(self, connection: Any) -> bool:
        try:
            await connection.scalar(select(1))
            await connection.commit()
        except Exception:
            return False
        return True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop)
        if self.provider is None:
            logger.info("context adapter idle: CP_CONTEXT_PROVIDER=none (enable with 'http')")
            await self._stop.wait()
            return
        lock_connection = await self._acquire_singleton_lock()
        if lock_connection is None:
            return
        logger.info("context adapter started (consumer=%s)", CONSUMER_NAME)
        try:
            while not self._stop.is_set():
                try:
                    # The advisory lock lives on its connection: if that
                    # session died, another replica may already hold the lock
                    # — stop consuming and re-acquire before continuing.
                    if not await self._lock_alive(lock_connection):
                        logger.warning("singleton lock connection lost; re-acquiring")
                        with contextlib.suppress(Exception):
                            await lock_connection.close()
                        lock_connection = await self._acquire_singleton_lock()
                        if lock_connection is None:
                            return
                    moved = await self.deliver_once()
                    delay = 0.0 if moved else self.settings.context_poll_interval_seconds
                except Exception:
                    # Per-tenant failures are handled inside deliver_once; this
                    # only catches an infrastructure-level cycle failure.
                    delay = min(
                        self.settings.context_retry_backoff_base_seconds * 2,
                        self.settings.context_retry_backoff_max_seconds,
                    )
                    logger.exception("context adapter cycle failed; retry in %.1fs", delay)
                if delay:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), delay)
        finally:
            await lock_connection.close()
            if self.provider is not None:
                await self.provider.aclose()
            await self.engine.dispose()
            logger.info("context adapter stopped")


def main() -> None:  # pragma: no cover - process entrypoint
    from control_plane.config import get_settings
    from control_plane.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(ContextAdapter(settings).run())


if __name__ == "__main__":  # pragma: no cover
    main()
