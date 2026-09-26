"""Example: follow the approvals of one workspace, once each, across restarts.

    uv pip install 'control-plane-client[events]' aiosqlite
    CP_URL=http://localhost:8000 CP_API_KEY=cp_... CP_WORKSPACE_ID=<uuid> \
        python approval_consumer.py

The cursor and the dedup record live in ``consumer.db`` next to the handler's
own table; kill the process at any moment and start it again — every
approval is recorded exactly once. The credential needs ``events.read`` on
the workspace (CP-ADR-0068). See docs/events/consumer.md.
"""

import asyncio
import json
import logging
import os
import signal

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert
from sqlalchemy.ext.asyncio import create_async_engine

from control_plane_client import ControlPlaneClient
from control_plane_client.events import Event, EventConsumer, to_cloudevent
from control_plane_client.events.sqlalchemy import SqlAlchemyCursorStore

metadata = MetaData()
decisions = Table(
    "decisions",
    metadata,
    Column("n", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),
    Column("cloudevent", Text, nullable=False),
)


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///consumer.db")
    store = SqlAlchemyCursorStore(engine, metadata=metadata)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    async def handler(event: Event) -> None:
        # Written in the store's transaction: committed together with the
        # cursor, or not at all.
        await store.session().execute(
            insert(decisions).values(
                event_id=event["id"], cloudevent=json.dumps(to_cloudevent(event))
            )
        )
        print(event["type"], event["payload"].get("taskTitle"))

    async with ControlPlaneClient(os.environ["CP_URL"], os.environ["CP_API_KEY"]) as client:
        consumer = EventConsumer(
            client,
            ["approval."],
            os.environ["CP_WORKSPACE_ID"],
            store,
            handler,
            name="example-approvals",
            start="latest",
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, consumer.stop)
        await consumer.run()
    await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
