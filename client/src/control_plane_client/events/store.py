"""Where an event consumer keeps its place in the journal (CP-ADR-0069).

The cursor belongs to the consumer, not to the server (CP-ADR-0068): the
server keeps no subscription state. A store keeps two things per consumer
name — the opaque cursor to resume from, and the ids of events already
handled, which make a re-delivered event a no-op instead of a second side
effect.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, runtime_checkable


@runtime_checkable
class CursorStore(Protocol):
    """Durable position and dedup record of named event consumers."""

    async def load(self, consumer: str) -> str | None:
        """The cursor to resume ``consumer`` from; ``None`` before its first event."""
        ...

    def handle(
        self, consumer: str, event_id: str, cursor: str
    ) -> AbstractAsyncContextManager[bool]:
        """One event, as a unit of work.

        Enters with ``True`` when ``event_id`` has not been handled by
        ``consumer`` yet, ``False`` when it has (the caller skips it). A clean
        exit records the id and moves the cursor to ``cursor`` — both or
        neither; an exit by exception records nothing, so the event comes
        again.
        """
        ...

    async def advance(self, consumer: str, cursor: str) -> None:
        """Move the cursor without an event (past what a filter left out)."""
        ...


class MemoryCursorStore:
    """In-process store: for tests and consumers that may start over.

    Nothing survives the process, so a restart replays the journal from the
    beginning (or from ``start="latest"``).
    """

    def __init__(self) -> None:
        self.cursors: dict[str, str] = {}
        self.handled: dict[str, set[str]] = {}

    async def load(self, consumer: str) -> str | None:
        return self.cursors.get(consumer)

    @asynccontextmanager
    async def handle(self, consumer: str, event_id: str, cursor: str) -> AsyncIterator[bool]:
        seen = self.handled.setdefault(consumer, set())
        fresh = event_id not in seen
        yield fresh
        seen.add(event_id)
        self.cursors[consumer] = cursor

    async def advance(self, consumer: str, cursor: str) -> None:
        self.cursors[consumer] = cursor
