"""Probe for test_parallel_runs.py; collected only when passed explicitly.

Each probe records the database its run works in, then waits until the
sibling probe has done the same, so both pytest runs are provably alive
(migrated, holding connections, truncating) at the same time.
"""

import os
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

BARRIER_TIMEOUT = 60.0


def test_probe_meets_its_sibling_in_another_database(sync_engine: Engine) -> None:
    barrier = Path(os.environ["CP_ISOLATION_PROBE_DIR"])
    with sync_engine.connect() as conn:
        database = conn.execute(text("SELECT current_database()")).scalar_one()
        # Holds a lock on a table a shared database would also be truncating.
        conn.execute(text("LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE"))
        (barrier / f"{os.getpid()}.db").write_text(database)
        deadline = time.monotonic() + BARRIER_TIMEOUT
        while len(list(barrier.glob("*.db"))) < 2:
            assert time.monotonic() < deadline, "the sibling run never reached the barrier"
            time.sleep(0.1)
        conn.rollback()
