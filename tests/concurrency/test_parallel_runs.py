"""Two pytest processes against one CP_TEST_DATABASE_URL do not block each other.

Each run gets its own database (tests/conftest.py); the probe holds an
exclusive table lock while waiting for its sibling, which would deadlock the
pair if they shared a database.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from tests.conftest import TEST_DATABASE_URL, run_database_pattern

PROBE = Path(__file__).with_name("isolation_probe.py")
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.timeout(300)
def test_two_parallel_runs_both_pass_in_separate_databases(tmp_path: Path) -> None:
    env = {**os.environ, "CP_ISOLATION_PROBE_DIR": str(tmp_path)}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(PROBE)]
    runs = [
        subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [run.communicate(timeout=240)[0] for run in runs]
    for run, output in zip(runs, outputs, strict=True):
        assert run.returncode == 0, output

    databases = {marker.read_text() for marker in tmp_path.glob("*.db")}
    base_name = make_url(TEST_DATABASE_URL).database or ""
    assert len(databases) == 2
    assert all(run_database_pattern(base_name).fullmatch(name) for name in databases)

    admin = create_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        with admin.connect() as conn:
            left = conn.execute(
                text("SELECT datname FROM pg_database WHERE datname = ANY(:names)"),
                {"names": sorted(databases)},
            ).all()
    finally:
        admin.dispose()
    assert left == [], "run databases must be dropped at session end"
