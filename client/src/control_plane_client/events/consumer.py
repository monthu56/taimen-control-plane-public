"""Event consumer: the journal as a subscription, on the consumer's side (CP-ADR-0069).

Pages come from ``GET /events`` with the subscription filters (CP-ADR-0068)
and the cursor of the store; every event is handled in a unit of work of the
store (dedup by ``event.id`` + the cursor move), so a restart in the middle of
a page resumes after the last handled event and handles nothing twice. The
WebSocket ``/events/ws`` only wakes the consumer up; the poll every
``poll_interval`` seconds covers a missing or broken socket. A failed page —
the server, the network or the handler — is read again from the stored
cursor after a backoff.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal
from urllib.parse import urlencode

from control_plane_client.client import ControlPlaneClient
from control_plane_client.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from control_plane_client.events.store import CursorStore

logger = logging.getLogger(__name__)

Event = dict[str, Any]
Handler = Callable[[Event], Awaitable[None]]

# A page that fails with these is a subscription the server refuses (no
# events.read on the workspace, an unknown workspace, a malformed filter, a
# credential it does not accept): reading it again changes nothing.
_REFUSED = (AuthenticationError, PermissionDeniedError, NotFoundError, ValidationError)

# WebSocket close codes after which the credential is renewed before reconnecting.
_WS_UNAUTHENTICATED = 4401


class HandlerError(Exception):
    """The handler failed on an event; the event is retried, never skipped."""

    def __init__(self, event: Event) -> None:
        super().__init__(f"handler failed on event {event.get('id')} ({event.get('type')})")
        self.event = event


class EventConsumer:
    """Handles the events of a filter once each, in journal order.

    ``types`` — event type prefixes (``approval.``), ``workspace_id`` — a
    workspace subtree; ``None``/empty reads the whole tenant (needs
    ``events.read`` on the tenant). ``name`` keys the cursor in the store:
    one name, one position; two processes under one name are serialized per
    event by the SQL store but still race on external side effects — run one.

    ``start`` matters only when the store has no cursor for ``name``:
    ``"earliest"`` handles the whole retained journal, ``"latest"`` begins
    after the last matching event at the first start.

    The handler raises to have the event retried (with backoff, from the
    stored cursor); to drop an event, it returns. :meth:`run` stops on
    :meth:`stop` or cancellation and raises when the server refuses the
    subscription itself.
    """

    def __init__(
        self,
        client: ControlPlaneClient,
        types: Sequence[str] | None,
        workspace_id: str | None,
        cursor_store: CursorStore,
        handler: Handler,
        *,
        name: str,
        start: Literal["earliest", "latest"] = "earliest",
        poll_interval: float = 30.0,
        page_size: int = 200,
        websocket: bool = True,
        retry_initial: float = 1.0,
        retry_max: float = 60.0,
    ) -> None:
        if not name:
            raise ValueError("an event consumer needs a name: it keys the stored cursor")
        self.client = client
        self.types = tuple(types or ())
        self.workspace_id = workspace_id
        self.store = cursor_store
        self.handler = handler
        self.name = name
        self.start = start
        self.poll_interval = poll_interval
        self.page_size = page_size
        self.websocket = websocket
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Finish the event in hand and return from :meth:`run`."""
        self._stopping.set()
        self._wake.set()

    def wake(self) -> None:
        """Read now instead of at the next poll (what a WebSocket frame does)."""
        self._wake.set()

    async def run(self) -> None:
        waker = asyncio.create_task(self._websocket_waker()) if self.websocket else None
        delay = self.retry_initial
        try:
            while not self._stopping.is_set():
                # Cleared before the read: a frame that arrives during the
                # read wakes the next wait instead of being lost.
                self._wake.clear()
                try:
                    await self.drain()
                except _REFUSED:
                    raise
                except Exception:
                    logger.warning(
                        "event consumer %s: page failed, retrying in %.1fs",
                        self.name,
                        delay,
                        exc_info=True,
                    )
                    await self._sleep(delay)
                    delay = min(delay * 2, self.retry_max)
                    continue
                delay = self.retry_initial
                await self._sleep(self.poll_interval, wakeable=True)
        finally:
            if waker is not None:
                waker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waker

    async def drain(self) -> int:
        """Handle everything readable now; the number of events handled.

        Raises what the first failed page or handler raised (a handler's
        exception wrapped in :class:`HandlerError`); everything handled
        before it stays recorded.
        """
        cursor = await self._position()
        handled = 0
        while not self._stopping.is_set():
            page = await self.client.list_events(
                cursor=cursor,
                types=self.types,
                workspace_id=self.workspace_id,
                limit=self.page_size,
            )
            for event in page.get("items", []):
                if self._stopping.is_set():
                    return handled
                async with self.store.handle(self.name, str(event["id"]), event["cursor"]) as fresh:
                    if fresh:
                        try:
                            await self.handler(event)
                        except Exception as exc:
                            raise HandlerError(event) from exc
                        handled += 1
            cursor = page["nextCursor"]
            await self.store.advance(self.name, cursor)
            if not page.get("hasMore"):
                break
        return handled

    async def _position(self) -> str | None:
        cursor = await self.store.load(self.name)
        if cursor is None and self.start == "latest":
            # The newest matching event is the last one this consumer will
            # not see; everything after it is new.
            page = await self.client.list_events(
                tail=1, types=self.types, workspace_id=self.workspace_id
            )
            cursor = str(page["nextCursor"])
            await self.store.advance(self.name, cursor)
        return cursor

    async def _sleep(self, seconds: float, *, wakeable: bool = False) -> None:
        signal = self._wake if wakeable else self._stopping
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(signal.wait(), seconds)

    # -- WebSocket wake-up ----------------------------------------------------

    def _websocket_url(self, cursor: str | None) -> str:
        base = self.client.server_url
        if base.startswith("https://"):
            base = "wss://" + base.removeprefix("https://")
        elif base.startswith("http://"):
            base = "ws://" + base.removeprefix("http://")
        query: list[tuple[str, str]] = [("after", cursor or "0")]
        query += [("types", t) for t in self.types]
        if self.workspace_id is not None:
            query.append(("workspaceId", self.workspace_id))
        return f"{base}/api/v1/events/ws?{urlencode(query)}"

    async def _websocket_waker(self) -> None:
        """Keep a socket open and turn every frame into a wake-up.

        Frames are not handled: the page read after the wake-up is the
        source of truth, so a frame lost with the socket costs latency, not
        an event. Without the ``websockets`` package the consumer polls.
        """
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import ConnectionClosed
        except ImportError:
            logger.info(
                "event consumer %s: websockets is not installed, polling every %.0fs",
                self.name,
                self.poll_interval,
            )
            return
        delay = self.retry_initial
        while not self._stopping.is_set():
            credential = self.client._credential
            try:
                # Subscribing from the stored cursor: the server sends the
                # backlog first, which only means one more (harmless) read.
                url = self._websocket_url(await self.store.load(self.name))
                async with connect(
                    url, additional_headers={"Authorization": f"Bearer {await credential.token()}"}
                ) as socket:
                    delay = self.retry_initial
                    async for _frame in socket:
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                logger.info("event consumer %s: websocket closed (%s)", self.name, code)
                if code == _WS_UNAUTHENTICATED and credential.refreshable:
                    with contextlib.suppress(Exception):
                        await credential.refresh()
            except Exception as exc:
                logger.info("event consumer %s: websocket unavailable: %s", self.name, exc)
            # A reconnect may have missed frames: read once to be sure.
            self._wake.set()
            await self._sleep(delay)
            delay = min(delay * 2, self.retry_max)
