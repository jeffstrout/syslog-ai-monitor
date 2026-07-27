"""API surface, including the homelab appliance contract endpoints."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app import db
from app.web import app

client = TestClient(app)


def test_api_version_reports_build_provenance():
    """Baked in by CI; reads `dev` on a local build."""
    body = client.get("/api/version").json()
    assert "commit" in body and "built_at" in body


def test_health_is_degraded_before_any_evaluation():
    """No successful evaluation means the service is alive but not doing its job.

    A plain liveness check would call this healthy, which is how a broken model
    call went unnoticed for 8.9 days.
    """
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["evaluation"]["healthy"] is False
    assert body["evaluation"]["last_success_age_seconds"] is None


def test_health_is_ok_after_a_recent_finding():
    db.insert_finding(overall_status="ok", summary="fine", log_count=1, payload={})
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["evaluation"]["healthy"] is True
    assert body["evaluation"]["last_success_age_seconds"] < 60


def test_health_reports_backlog_hours(insert_log_at):
    insert_log_at(time.time() - 2 * 3600, "old line")
    body = client.get("/api/health").json()

    assert body["buffered_logs"] == 1
    assert 1.5 < body["evaluation"]["backlog_hours"] < 2.5


def test_health_exposes_the_commit():
    assert "commit" in client.get("/api/health").json()


def test_history_is_empty_before_any_evaluation():
    assert client.get("/api/history").json()["findings"] == []


def test_latest_returns_the_most_recent_finding():
    db.insert_finding(overall_status="ok", summary="older", log_count=1, payload={})
    db.insert_finding(overall_status="error", summary="newer", log_count=2, payload={})

    assert client.get("/api/latest").json()["summary"] == "newer"
