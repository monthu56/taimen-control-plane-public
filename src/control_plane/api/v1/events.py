"""Event journal endpoints: paged reads and a resumable WebSocket stream.

The WebSocket is a wake-up channel, not a source of truth: the server always
reads events from the ``events`` table in ``(tx_id, sequence)`` delivery
order. Clients track the opaque ``cursor`` of the last processed event and
reconnect with ``?after=<cursor>`` to resume; a periodic poll covers lost
NOTIFYs. Legacy v0.3 integer sequences are still accepted as ``after``
values (adapted server-side, see application/event_cursor.py).

Both readers take the same narrowing filters (CP-ADR-0068): ``types`` —
event type prefixes, ``workspaceId`` — the events of a workspace subtree,
``events.read`` checked on that workspace. A filter never changes the order
or the meaning of a cursor: a filtered reader resumes where it stopped.

The page reads backward too (CP-ADR-0024, amendment 2026-09-29):
``before=<cursor>`` gives the events strictly before it, ``prevCursor`` of a
page is the ``before`` of the preceding one; ``order=desc`` only flips the
items of a page, never which events it holds.
"""

import asyncio
import contextlib
import logging
import uuid
from typing import Literal, cast

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane import observability
from control_plane.api.dependencies import AuthDep, DbDep, authenticate_websocket
from control_plane.api.v1.schemas import ERROR_RESPONSES, EventOut, EventPageOut, dump
from control_plane.application.authorization import AuthContext
from control_plane.application.event_cursor import (
    EventCursor,
    EventPosition,
    decode_cursor,
    encode_position,
)
from control_plane.application.queries import events as queries
from control_plane.application.queries.events import (
    EventFilter,
    JournalEvent,
    parse_type_prefixes,
)
from control_plane.config import Settings
from control_plane.domain.errors import DomainError
from control_plane.infrastructure.realtime.hub import RealtimeHub

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_WS_BATCH_LIMIT = 200


def _event_body(event: JournalEvent) -> dict[str, object]:
    return dump(EventOut, event, cursor=encode_position(queries.position_of(event)))


@router.get("/events", response_model=EventPageOut, responses=ERROR_RESPONSES)
async def list_events(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    after: int | None = Query(default=None, ge=0),
    before: str | None = Query(default=None),
    order: Literal["asc", "desc"] = Query(default="asc"),
    tail: int | None = Query(default=None, ge=1),
    entity_type: str | None = Query(default=None, alias="entityType"),
    entity_id: uuid.UUID | None = Query(default=None, alias="entityId"),
    types: list[str] | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
) -> JSONResponse:
    page = await queries.list_events(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        after=after,
        before=before,
        tail=tail,
        entity_type=entity_type,
        entity_id=entity_id,
        types=parse_type_prefixes(types),
        workspace_id=workspace_id,
    )
    observability.inc("event_replay_requests_total")
    observability.inc("event_replay_events_total", len(page.events))
    items = [_event_body(e) for e in page.events]
    if order == "desc":
        items.reverse()
    return JSONResponse(
        {
            "items": items,
            "nextCursor": page.next_cursor,
            "prevCursor": page.prev_cursor,
            "hasMore": page.has_more,
        }
    )


async def _drain_client(websocket: WebSocket) -> None:
    """Consume client frames (acks/keepalives) until the client disconnects."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


# authz: public — аутентификация в обработчике (authenticate_websocket) + events.read
@router.websocket("/events/ws")
async def events_ws(
    websocket: WebSocket,
    after: str = Query(default="0"),
    types: list[str] | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
) -> None:
    settings = cast(Settings, websocket.app.state.settings)
    session_factory = cast(async_sessionmaker[AsyncSession], websocket.app.state.session_factory)
    hub = cast(RealtimeHub, websocket.app.state.realtime_hub)

    ctx: AuthContext | None = None
    filters = EventFilter()
    close_code: int | None = None
    start: EventCursor = EventPosition(0, 0)
    try:
        start = decode_cursor(after)
        prefixes = parse_type_prefixes(types)
        ctx = await authenticate_websocket(websocket, settings, session_factory)
        async with session_factory() as db:
            filters = await queries.authorize_event_read(
                db, ctx, workspace_id=workspace_id, types=prefixes
            )
    except DomainError as exc:
        if exc.http_status == 401:
            close_code = 4401
        elif exc.http_status == 403:
            close_code = 4403
        elif exc.http_status == 404:
            close_code = 4404  # the workspace of the filter does not exist
        elif exc.http_status == 503:
            # No decision was reached rather than access refused: retrying is
            # meaningful for the client.
            close_code = 4503
        else:
            close_code = 4400  # malformed/unsupported cursor or filter

    # Close codes only reach the client after the handshake completes, so
    # accept first even on auth failure, then close with 4401/4403.
    await websocket.accept()
    if close_code is not None or ctx is None:
        await websocket.close(code=close_code or 4401)
        return

    reader = asyncio.create_task(_drain_client(websocket))
    waiter: asyncio.Task[None] | None = None
    try:
        while not reader.done():
            # Drain everything new before going back to sleep.
            while True:
                async with session_factory() as db:
                    frontier = (
                        await queries.current_position(db, ctx.tenant_id)
                        if filters.narrows
                        else None
                    )
                    events = await queries.fetch_events_after(
                        db,
                        tenant_id=ctx.tenant_id,
                        start=start,
                        limit=_WS_BATCH_LIMIT,
                        filters=filters,
                    )
                for event in events:
                    await websocket.send_json(_event_body(event))
                    start = queries.position_of(event)
                if len(events) < _WS_BATCH_LIMIT:
                    start = queries.past_filtered_out(start, frontier)
                    break

            waiter = asyncio.create_task(
                hub.wait(ctx.tenant_id, timeout_seconds=settings.ws_poll_interval_seconds)
            )
            done, _ = await asyncio.wait({waiter, reader}, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                break
    except (WebSocketDisconnect, RuntimeError):
        pass  # client went away mid-send
    except Exception:
        logger.exception("events websocket failed")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        for task in (reader, waiter):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
