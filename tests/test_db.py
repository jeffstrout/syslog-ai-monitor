"""Raw-log buffer behaviour — the bounds that prevent a repeat of 2026-07-27."""
from __future__ import annotations

import time

from app import db


def test_fetch_logs_until_caps_the_batch(insert_log_at):
    """Without a cap, one run loads the whole table — 15.9M rows, in the incident."""
    now = time.time()
    for i in range(50):
        insert_log_at(now - 100 + i, f"line {i}")

    assert len(db.fetch_logs_until(now + 1)) == 50            # uncapped
    assert len(db.fetch_logs_until(now + 1, limit=10)) == 10  # capped


def test_capped_batch_returns_the_oldest_rows_first(insert_log_at):
    """The cap must take the oldest rows, or the backlog never drains."""
    now = time.time()
    for i in range(20):
        insert_log_at(now - 100 + i, f"line {i}")

    batch = db.fetch_logs_until(now + 1, limit=5)
    assert [r["message"] for r in batch] == [f"line {i}" for i in range(5)]


def test_delete_through_id_leaves_newer_rows(insert_log_at):
    """This is why deletion is by id, not by timestamp.

    A capped batch evaluates only part of the buffer; deleting by the original
    `cutoff` would discard rows that were never looked at.
    """
    now = time.time()
    for i in range(10):
        insert_log_at(now - 100 + i, f"line {i}")

    batch = db.fetch_logs_until(now + 1, limit=4)
    removed = db.delete_logs_through_id(max(r["id"] for r in batch))

    assert removed == 4
    assert db.raw_log_count() == 6
    remaining = db.fetch_logs_until(now + 1)
    assert [r["message"] for r in remaining] == [f"line {i}" for i in range(4, 10)]


def test_delete_logs_until_is_inclusive_of_the_cutoff(insert_log_at):
    now = time.time()
    insert_log_at(now - 200, "old")
    insert_log_at(now - 100, "boundary")
    insert_log_at(now - 50, "new")

    assert db.delete_logs_until(now - 100) == 2      # <= cutoff
    assert [r["message"] for r in db.fetch_logs_until(now + 1)] == ["new"]


def test_raw_log_oldest_ts_is_none_when_empty(insert_log_at):
    assert db.raw_log_oldest_ts() is None

    now = time.time()
    insert_log_at(now - 500, "old")
    insert_log_at(now - 10, "recent")
    assert db.raw_log_oldest_ts() == now - 500
