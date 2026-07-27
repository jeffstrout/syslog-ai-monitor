"""The safety bounds added after the 2026-07-27 brownout.

Context: the Anthropic account ran out of credit, every hourly run failed with a
400, and because raw logs are deliberately kept on failure the table reached
15.9M rows / 5.0 GB over 8.9 days. Each run then spent ~41 minutes on work that
could not succeed.

No test here performs a real model call — `claude_client.evaluate` is always
replaced.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app import db, evaluator
from app.config import settings


class _ApiError(Exception):
    """Stand-in for an anthropic SDK error, which carries `status_code`."""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


# --- error classification ---------------------------------------------------

def test_billing_and_auth_errors_are_permanent():
    """A 400 'credit balance is too low' fails identically every hour."""
    assert evaluator._is_permanent_api_error(_ApiError(400)) is True
    assert evaluator._is_permanent_api_error(_ApiError(401)) is True
    assert evaluator._is_permanent_api_error(_ApiError(403)) is True


def test_rate_limit_is_not_permanent():
    """429 is the one 4xx worth retrying."""
    assert evaluator._is_permanent_api_error(_ApiError(429)) is False


def test_server_errors_and_unknowns_are_not_permanent():
    assert evaluator._is_permanent_api_error(_ApiError(500)) is False
    assert evaluator._is_permanent_api_error(_ApiError(503)) is False
    # A plain exception has no status_code and must not be treated as permanent.
    assert evaluator._is_permanent_api_error(ValueError("boom")) is False


# --- the age bound ----------------------------------------------------------

def test_bound_drops_rows_older_than_the_limit(insert_log_at):
    now = time.time()
    beyond = settings.raw_log_max_age_hours * 3600 + 3600
    insert_log_at(now - beyond, "ancient")
    insert_log_at(now - 60, "recent")

    evaluator._enforce_raw_log_bound()

    assert [r["message"] for r in db.fetch_logs_until(now + 1)] == ["recent"]


def test_bound_keeps_everything_inside_the_window(insert_log_at):
    """The retry behaviour must survive — only the excess is dropped."""
    now = time.time()
    inside = settings.raw_log_max_age_hours * 3600 - 3600
    insert_log_at(now - inside, "old but within the window")
    insert_log_at(now - 60, "recent")

    evaluator._enforce_raw_log_bound()

    assert db.raw_log_count() == 2


def test_bound_is_disabled_when_set_to_zero(insert_log_at, monkeypatch):
    """0 means 'no bound' — an explicit escape hatch.

    `settings` is a frozen dataclass, so the module reference is swapped rather
    than the attribute mutated.
    """
    now = time.time()
    insert_log_at(now - 400 * 3600, "very old")

    monkeypatch.setattr(evaluator, "settings",
                        SimpleNamespace(raw_log_max_age_hours=0))
    evaluator._enforce_raw_log_bound()

    assert db.raw_log_count() == 1


# --- run_evaluation, the incident scenario ----------------------------------

def test_failed_evaluation_keeps_recent_logs_and_drops_stale_ones(
        insert_log_at, monkeypatch):
    """Exactly what happened on 2026-07-27, with the bound now in place.

    The window must survive for the next retry, but the buffer must not grow
    without limit.
    """
    now = time.time()
    beyond = settings.raw_log_max_age_hours * 3600 + 3600
    insert_log_at(now - beyond, "ancient")
    insert_log_at(now - 60, "recent")

    def _fail(*_a, **_k):
        raise _ApiError(400)

    alerted: list[tuple[bool, int]] = []
    monkeypatch.setattr(evaluator.claude_client, "evaluate", _fail)
    monkeypatch.setattr(
        evaluator.alerts, "maybe_send_failure",
        lambda exc, permanent, buffered: alerted.append((permanent, buffered)),
    )

    result = evaluator.run_evaluation()

    assert result is None
    assert [r["message"] for r in db.fetch_logs_until(now + 1)] == ["recent"]
    assert alerted and alerted[0][0] is True   # classified as permanent


def test_successful_evaluation_stores_a_finding_and_purges_its_batch(
        insert_log_at, monkeypatch):
    now = time.time()
    for i in range(5):
        insert_log_at(now - 100 + i, f"line {i}")

    payload = {"overall_status": "ok", "summary": "nothing of note", "findings": []}
    monkeypatch.setattr(evaluator.claude_client, "evaluate",
                        lambda *_a, **_k: payload)
    monkeypatch.setattr(evaluator.alerts, "maybe_send", lambda _r: None)

    result = evaluator.run_evaluation()

    assert result == payload
    assert db.raw_log_count() == 0
    assert db.findings_count() == 1
    assert db.latest_finding()["summary"] == "nothing of note"
