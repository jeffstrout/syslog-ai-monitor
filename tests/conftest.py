"""Test fixtures.

`config.Settings` is a frozen dataclass whose defaults are read from the
environment **at import time**, so DB_PATH has to be set before anything under
`app.` is imported. conftest.py runs first, which is what makes this work.
"""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="syslog-tests-")
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))
# The key is never used — no test performs a real model call — but its absence
# logs a warning that muddies output.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-unused")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """Truncate every table between tests.

    `db` keeps a single module-level connection, so tests share one file rather
    than one per test; truncating is simpler than tearing the connection down.
    """
    from app import db

    db.init()
    with db._lock:
        conn = db._db()
        for table in ("raw_logs", "findings", "weekly_summaries"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    yield


@pytest.fixture
def insert_log_at():
    """Insert a raw log at an explicit timestamp.

    `db.insert_log` always stamps `now`, so backdating needs direct SQL — which
    every bound-related test depends on.
    """
    from app import db

    def _insert(ts: float, message: str = "test message", severity: int = 6,
                host: str = "testhost") -> None:
        with db._lock:
            db._db().execute(
                "INSERT INTO raw_logs (ts, host, facility, severity, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, host, 1, severity, message),
            )
            db._db().commit()

    return _insert
