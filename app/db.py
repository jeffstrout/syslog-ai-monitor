"""SQLite storage: ephemeral raw_logs + retained findings.

raw_logs holds syslog lines only until the next hourly evaluation, after which
they are deleted. findings holds the AI evaluation results (the history shown in
the dashboard) and is purged on a retention schedule.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from .config import settings

# A single connection guarded by a lock keeps this simple and correct for our
# modest write volume (syslog inserts + one evaluation per hour). The lock is
# re-entrant because helpers acquire it and may call _db() -> init(), which
# acquires it again (e.g. a request arriving before main.py's init()).
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init() -> None:
    global _conn
    with _lock:
        _conn = _connect()
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL NOT NULL,          -- unix epoch received
                host      TEXT,
                facility  INTEGER,
                severity  INTEGER,                -- syslog numeric severity 0-7
                message   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_logs(ts);

            CREATE TABLE IF NOT EXISTS findings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,     -- unix epoch of evaluation
                overall_status  TEXT NOT NULL,     -- ok | warning | error
                summary         TEXT NOT NULL,
                log_count       INTEGER NOT NULL,  -- raw lines evaluated
                payload_json    TEXT NOT NULL      -- full structured result
            );
            CREATE INDEX IF NOT EXISTS idx_findings_ts ON findings(ts);

            CREATE TABLE IF NOT EXISTS weekly_summaries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,     -- unix epoch of the review
                period_start    REAL NOT NULL,     -- window start (epoch)
                period_end      REAL NOT NULL,     -- window end (epoch)
                window_days     INTEGER NOT NULL,
                finding_count   INTEGER NOT NULL,  -- hourly findings reviewed
                payload_json    TEXT NOT NULL      -- full structured result
            );
            CREATE INDEX IF NOT EXISTS idx_weekly_ts ON weekly_summaries(ts);
            """
        )
        _conn.commit()


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


# ── raw_logs ────────────────────────────────────────────────────────────────

def insert_log(host: str | None, facility: int | None, severity: int | None,
               message: str) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO raw_logs (ts, host, facility, severity, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), host, facility, severity, message),
        )
        _db().commit()


def fetch_logs_until(cutoff_ts: float) -> list[sqlite3.Row]:
    """Return all raw logs with ts <= cutoff (the batch to evaluate)."""
    with _lock:
        cur = _db().execute(
            "SELECT id, ts, host, facility, severity, message "
            "FROM raw_logs WHERE ts <= ? ORDER BY ts",
            (cutoff_ts,),
        )
        return cur.fetchall()


def delete_logs_until(cutoff_ts: float) -> int:
    """Delete the evaluated batch; returns rows removed."""
    with _lock:
        cur = _db().execute("DELETE FROM raw_logs WHERE ts <= ?", (cutoff_ts,))
        _db().commit()
        return cur.rowcount


def raw_log_count() -> int:
    with _lock:
        return _db().execute("SELECT COUNT(*) FROM raw_logs").fetchone()[0]


# ── findings ────────────────────────────────────────────────────────────────

def insert_finding(overall_status: str, summary: str, log_count: int,
                   payload: dict[str, Any]) -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO findings (ts, overall_status, summary, log_count, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), overall_status, summary, log_count, json.dumps(payload)),
        )
        _db().commit()
        return int(cur.lastrowid)


def _row_to_finding(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "overall_status": row["overall_status"],
        "summary": row["summary"],
        "log_count": row["log_count"],
        "payload": json.loads(row["payload_json"]),
    }


def latest_finding() -> dict[str, Any] | None:
    with _lock:
        row = _db().execute(
            "SELECT * FROM findings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return _row_to_finding(row) if row else None


def list_findings(limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute(
            "SELECT * FROM findings ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_finding(r) for r in rows]


def findings_count() -> int:
    with _lock:
        return _db().execute("SELECT COUNT(*) FROM findings").fetchone()[0]


def fetch_findings_since(since_ts: float) -> list[dict[str, Any]]:
    """Return findings with ts >= since (oldest first) for the weekly review."""
    with _lock:
        rows = _db().execute(
            "SELECT * FROM findings WHERE ts >= ? ORDER BY ts", (since_ts,)
        ).fetchall()
    return [_row_to_finding(r) for r in rows]


def purge_findings(older_than_ts: float) -> int:
    with _lock:
        cur = _db().execute("DELETE FROM findings WHERE ts < ?", (older_than_ts,))
        _db().commit()
        return cur.rowcount


# ── weekly_summaries ────────────────────────────────────────────────────────

def insert_weekly(period_start: float, period_end: float, window_days: int,
                  finding_count: int, payload: dict[str, Any]) -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO weekly_summaries "
            "(ts, period_start, period_end, window_days, finding_count, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), period_start, period_end, window_days, finding_count,
             json.dumps(payload)),
        )
        _db().commit()
        return int(cur.lastrowid)


def _row_to_weekly(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "window_days": row["window_days"],
        "finding_count": row["finding_count"],
        "payload": json.loads(row["payload_json"]),
    }


def latest_weekly() -> dict[str, Any] | None:
    with _lock:
        row = _db().execute(
            "SELECT * FROM weekly_summaries ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return _row_to_weekly(row) if row else None


def list_weekly(limit: int = 30) -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute(
            "SELECT * FROM weekly_summaries ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_weekly(r) for r in rows]


def purge_weekly(older_than_ts: float) -> int:
    with _lock:
        cur = _db().execute(
            "DELETE FROM weekly_summaries WHERE ts < ?", (older_than_ts,)
        )
        _db().commit()
        return cur.rowcount
