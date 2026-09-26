"""Event consumer SDK: the journal as a subscription (CP-ADR-0068, CP-ADR-0069).

``EventConsumer`` reads the events of a filter in journal order, handles each
once and resumes where it stopped; ``CursorStore`` is where it keeps its
place (``MemoryCursorStore`` here, ``SqlAlchemyCursorStore`` in
``control_plane_client.events.sqlalchemy``, which needs the ``sqlalchemy``
extra); ``to_cloudevent`` exports an event as CloudEvents 1.0.
"""

from control_plane_client.events.cloudevents import default_source, to_cloudevent
from control_plane_client.events.consumer import Event, EventConsumer, Handler, HandlerError
from control_plane_client.events.store import CursorStore, MemoryCursorStore

__all__ = [
    "CursorStore",
    "Event",
    "EventConsumer",
    "Handler",
    "HandlerError",
    "MemoryCursorStore",
    "default_source",
    "to_cloudevent",
]
