"""Circuit breaker (#12).

A permanent model error cannot clear on retry, so re-fetching and re-templatizing
the whole buffer every hour burns ~90 s of GIL-holding CPU on work guaranteed to
fail. The breaker skips that work until a backoff expires.
"""
from __future__ import annotations

import time

from app import db, evaluator
from app.config import settings


class _ApiError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _fail_with(monkeypatch, status: int) -> list[int]:
    """Make the model call raise, and record how many times it was reached."""
    calls: list[int] = []

    def _raise(*_a, **_k):
        calls.append(1)
        raise _ApiError(status)

    monkeypatch.setattr(evaluator.claude_client, "evaluate", _raise)
    monkeypatch.setattr(evaluator.alerts, "maybe_send_failure",
                        lambda *_a, **_k: None)
    return calls


def _succeed(monkeypatch) -> list[int]:
    calls: list[int] = []
    payload = {"overall_status": "ok", "summary": "fine", "findings": []}

    def _ok(*_a, **_k):
        calls.append(1)
        return payload

    monkeypatch.setattr(evaluator.claude_client, "evaluate", _ok)
    monkeypatch.setattr(evaluator.alerts, "maybe_send", lambda _r: None)
    return calls


# --- opening ----------------------------------------------------------------

def test_permanent_failure_opens_the_breaker(insert_log_at, monkeypatch):
    insert_log_at(time.time() - 60, "a line")
    _fail_with(monkeypatch, 400)

    evaluator.run_evaluation()

    state = evaluator.breaker_state()
    assert state["open"] is True
    assert state["consecutive_permanent_failures"] == 1
    assert state["retry_in_seconds"] > 0


def test_transient_failure_does_not_open_the_breaker(insert_log_at, monkeypatch):
    """A 429 or a 500 should just retry on the next tick."""
    insert_log_at(time.time() - 60, "a line")
    _fail_with(monkeypatch, 429)

    evaluator.run_evaluation()

    assert evaluator.breaker_state()["open"] is False


# --- skipping ---------------------------------------------------------------

def test_open_breaker_skips_the_model_call_entirely(insert_log_at, monkeypatch):
    """The point of the feature: no fetch, no digest, no call."""
    insert_log_at(time.time() - 60, "a line")
    calls = _fail_with(monkeypatch, 400)

    evaluator.run_evaluation()          # trips it
    assert len(calls) == 1

    for _ in range(5):                  # would be five more hours of doomed work
        assert evaluator.run_evaluation() is None
    assert len(calls) == 1, "breaker did not prevent further model calls"


def test_open_breaker_does_not_build_a_digest(insert_log_at, monkeypatch):
    """Digesting is the expensive half — it must be skipped, not just the call."""
    insert_log_at(time.time() - 60, "a line")
    _fail_with(monkeypatch, 400)
    evaluator.run_evaluation()

    digests: list[int] = []
    monkeypatch.setattr(evaluator, "build_digest",
                        lambda rows: digests.append(1) or ("", {}))

    evaluator.run_evaluation()
    assert digests == []


def test_open_breaker_still_enforces_the_raw_log_bound(insert_log_at, monkeypatch):
    """The breaker must not become a new way for the buffer to grow."""
    now = time.time()
    insert_log_at(now - 60, "recent")
    _fail_with(monkeypatch, 400)
    evaluator.run_evaluation()

    insert_log_at(now - (settings.raw_log_max_age_hours * 3600 + 3600), "ancient")
    evaluator.run_evaluation()          # skipped by the breaker

    assert [r["message"] for r in db.fetch_logs_until(now + 1)] == ["recent"]


# --- bypass and recovery ----------------------------------------------------

def test_force_bypasses_an_open_breaker(insert_log_at, monkeypatch):
    """/api/run-now is a deliberate probe — it must always attempt the call."""
    insert_log_at(time.time() - 60, "a line")
    calls = _fail_with(monkeypatch, 400)
    evaluator.run_evaluation()
    assert len(calls) == 1

    evaluator.run_evaluation(force=True)
    assert len(calls) == 2


def test_success_resets_the_breaker(insert_log_at, monkeypatch):
    """Recovery is automatic once the underlying problem clears."""
    insert_log_at(time.time() - 60, "a line")
    _fail_with(monkeypatch, 400)
    evaluator.run_evaluation()
    assert evaluator.breaker_state()["open"] is True

    _succeed(monkeypatch)
    evaluator.run_evaluation(force=True)

    state = evaluator.breaker_state()
    assert state["open"] is False
    assert state["consecutive_permanent_failures"] == 0


# --- backoff ----------------------------------------------------------------

def test_backoff_doubles_then_caps():
    base = settings.breaker_backoff_base_minutes * 60
    cap = settings.breaker_backoff_max_hours * 3600

    assert evaluator._breaker_backoff_seconds(1) == base
    assert evaluator._breaker_backoff_seconds(2) == base * 2
    assert evaluator._breaker_backoff_seconds(3) == base * 4
    # Far enough out that doubling would exceed the cap.
    assert evaluator._breaker_backoff_seconds(20) == cap


def test_repeated_permanent_failures_lengthen_the_backoff(insert_log_at, monkeypatch):
    insert_log_at(time.time() - 60, "a line")
    _fail_with(monkeypatch, 400)

    evaluator.run_evaluation()
    first = evaluator.breaker_state()["retry_in_seconds"]

    evaluator.run_evaluation(force=True)   # bypass, fail again
    second = evaluator.breaker_state()["retry_in_seconds"]

    assert second > first
    assert evaluator.breaker_state()["consecutive_permanent_failures"] == 2


def test_breaker_state_is_reported_when_closed():
    state = evaluator.breaker_state()
    assert state == {
        "open": False,
        "retry_in_seconds": None,
        "consecutive_permanent_failures": 0,
    }
