"""Cursor store on the consumer's own database (SQLAlchemy 2, async).

Needs the ``sqlalchemy`` extra: ``control-plane-client[sqlalchemy]`` plus the
async driver of the database (``psycopg``, ``asyncpg``, ``aiosqlite``).

An event is handled inside one transaction of this store: the dedup record,
the cursor and — when the handler writes through :meth:`SqlAlchemyCursorStore.session`
— the handler's own rows commit together. A crash anywhere before that commit
leaves no trace, and the event is handled again after the restart; a crash
after it leaves the event recorded, and it is skipped. That is exactly-once
for the effects in this database; effects outside it (a message sent to a
chat) are at-least-once, keyed by the event id.
"""

import contextvars
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_current: contextvars.ContextVar[AsyncSession | None] = contextvars.ContextVar(
    "control_plane_event_session", default=None
)


def cursor_tables(metadata: MetaData, *, prefix: str = "") -> tuple[Table, Table]:
    """Declare the store's two tables on ``metadata``.

    Pass the metadata the consumer's migrations are generated from (Alembic
    ``target_metadata``) to get the tables into its schema history, or call
    :meth:`SqlAlchemyCursorStore.create_tables` for a schema without
    migrations. ``prefix`` keeps the names clear of the consumer's own tables.
    """
    cursors = Table(
        f"{prefix}event_cursors",
        metadata,
        Column("consumer", String(200), primary_key=True),
        Column("cursor", Text, nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    handled = Table(
        f"{prefix}handled_events",
        metadata,
        Column("consumer", String(200), primary_key=True),
        Column("event_id", String(64), primary_key=True),
        Column("handled_at", DateTime(timezone=True), nullable=False),
        Index(f"ix_{prefix}handled_events_consumer_handled_at", "consumer", "handled_at"),
    )
    return cursors, handled


class SqlAlchemyCursorStore:
    """:class:`~control_plane_client.events.CursorStore` in SQL tables.

    ``dedup_retention`` bounds the dedup record: a re-delivery happens next to
    the cursor (a retried page, a server that errs toward re-delivery), never
    days behind it, so older ids are pruned when the cursor advances.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        metadata: MetaData | None = None,
        prefix: str = "",
        dedup_retention: timedelta = timedelta(days=7),
        prune_interval: float = 600.0,
    ) -> None:
        self.metadata = metadata if metadata is not None else MetaData()
        self.cursors, self.handled = cursor_tables(self.metadata, prefix=prefix)
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._retention = dedup_retention
        self._prune_interval = prune_interval
        self._pruned_at = 0.0

    async def create_tables(self) -> None:
        """Create the store's tables if they are missing (no migrations)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(
                lambda sync: self.metadata.create_all(sync, tables=[self.cursors, self.handled])
            )

    @staticmethod
    def session() -> AsyncSession:
        """The transaction of the event being handled, for the handler's writes.

        Rows written through it commit together with the dedup record and the
        cursor, and roll back with them. Only valid inside a handler.
        """
        session = _current.get()
        if session is None:
            raise RuntimeError("SqlAlchemyCursorStore.session() is only valid inside a handler")
        return session

    async def load(self, consumer: str) -> str | None:
        async with self._sessions() as session:
            cursor: str | None = await session.scalar(
                select(self.cursors.c.cursor).where(self.cursors.c.consumer == consumer)
            )
            return cursor

    @asynccontextmanager
    async def handle(self, consumer: str, event_id: str, cursor: str) -> AsyncIterator[bool]:
        async with self._sessions() as session, session.begin():
            # Serializes two instances of one consumer on the cursor row (a
            # no-op on SQLite); a first-ever race ends in a primary-key
            # conflict on commit, which the consumer retries as an error.
            await session.execute(
                select(self.cursors.c.consumer)
                .where(self.cursors.c.consumer == consumer)
                .with_for_update()
            )
            seen = await session.scalar(
                select(self.handled.c.event_id).where(
                    self.handled.c.consumer == consumer, self.handled.c.event_id == event_id
                )
            )
            if seen is not None:
                await self._set_cursor(session, consumer, cursor)
                yield False
                return
            token = _current.set(session)
            try:
                yield True
            finally:
                _current.reset(token)
            await session.execute(
                insert(self.handled).values(
                    consumer=consumer, event_id=event_id, handled_at=datetime.now(UTC)
                )
            )
            await self._set_cursor(session, consumer, cursor)

    async def advance(self, consumer: str, cursor: str) -> None:
        async with self._sessions() as session, session.begin():
            await self._set_cursor(session, consumer, cursor)
            if time.monotonic() - self._pruned_at >= self._prune_interval:
                self._pruned_at = time.monotonic()
                await session.execute(
                    delete(self.handled).where(
                        self.handled.c.consumer == consumer,
                        self.handled.c.handled_at < datetime.now(UTC) - self._retention,
                    )
                )

    async def _set_cursor(self, session: AsyncSession, consumer: str, cursor: str) -> None:
        now = datetime.now(UTC)
        result = await session.execute(
            update(self.cursors)
            .where(self.cursors.c.consumer == consumer)
            .values(cursor=cursor, updated_at=now)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            await session.execute(
                insert(self.cursors).values(consumer=consumer, cursor=cursor, updated_at=now)
            )
