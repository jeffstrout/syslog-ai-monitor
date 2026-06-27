"""FastAPI app: JSON API + the static dashboard.

Interactive, auto-generated API docs are available at runtime:
  - Swagger UI : http://<host>:8080/docs
  - ReDoc      : http://<host>:8080/redoc
  - OpenAPI    : http://<host>:8080/openapi.json
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, evaluator

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_STARTED_AT = time.time()

app = FastAPI(
    title="Syslog AI Monitor",
    version="1.0.0",
    description=(
        "Receives UniFi UDM Pro syslog, evaluates it hourly with Claude Haiku, "
        "and exposes the findings.\n\n"
        "Raw logs are ephemeral and discarded after each evaluation; only the "
        "structured AI **findings** are retained (default 30 days). All endpoints "
        "are unauthenticated and intended for use on a trusted LAN."
    ),
    openapi_tags=[
        {"name": "monitoring", "description": "Read evaluation results and status."},
        {"name": "actions", "description": "Trigger work on demand."},
        {"name": "ui", "description": "The dashboard front-end."},
    ],
)


@app.get("/api/health", tags=["monitoring"], summary="Health check")
def health() -> JSONResponse:
    """Service health for uptime monitors.

    Returns `200` with `{"status": "ok", ...}` when the service and database are
    reachable, or `503` with `{"status": "error", ...}` if the database can't be
    queried.

    Example:
    ```json
    {
      "status": "ok",
      "version": "1.0.0",
      "uptime_seconds": 3725,
      "buffered_logs": 842,
      "findings_stored": 17,
      "last_evaluation_ts": 1750896000.0
    }
    ```
    """
    try:
        latest = db.latest_finding()
        return JSONResponse({
            "status": "ok",
            "version": app.version,
            "uptime_seconds": round(time.time() - _STARTED_AT),
            "buffered_logs": db.raw_log_count(),
            "findings_stored": db.findings_count(),
            "last_evaluation_ts": latest["ts"] if latest else None,
        })
    except Exception as exc:  # database unreachable / corrupt
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(exc)},
        )


@app.get("/api/status", tags=["monitoring"], summary="Overview")
def status() -> dict:
    """Lightweight overview for the dashboard header.

    Returns the number of raw log lines currently buffered (awaiting the next
    evaluation) and the most recent finding (or `null` if none exist yet).

    Example response:
    ```json
    {
      "buffered_logs": 1342,
      "latest": { "id": 7, "ts": 1750896000.0, "overall_status": "ok",
                  "summary": "Healthy hour.", "log_count": 4985, "payload": { ... } }
    }
    ```
    """
    return {
        "buffered_logs": db.raw_log_count(),
        "latest": db.latest_finding(),
    }


@app.get("/api/latest", tags=["monitoring"], summary="Most recent finding")
def latest() -> JSONResponse:
    """Return the single most recent finding object, or `null` if none exist.

    A finding object has the shape:
    ```json
    {
      "id": 7,
      "ts": 1750896000.0,            // unix epoch of the evaluation
      "overall_status": "ok|warning|error",
      "summary": "one-line human summary",
      "log_count": 4985,             // raw lines evaluated
      "payload": {                   // the full structured AI result
        "overall_status": "ok|warning|error",
        "summary": "...",
        "findings": [ /* see /api/history */ ]
      }
    }
    ```
    """
    return JSONResponse(db.latest_finding())


@app.get("/api/history", tags=["monitoring"], summary="Finding history")
def history(limit: int = 200) -> dict:
    """Return recent findings, newest first.

    Query params:
      - `limit` (int, default 200): maximum number of findings to return.

    Each item in `findings` matches the `/api/latest` shape. Every individual
    issue inside `payload.findings` looks like:
    ```json
    {
      "severity": "info|warning|error|critical",
      "category": "VPN Connectivity",
      "title": "OpenVPN client persistent connection failure",
      "detail": "what is happening and why it matters",
      "evidence": "representative log line(s)",
      "occurrences": 28,
      "recommendation": "suggested action"
    }
    ```
    """
    return {"findings": db.list_findings(limit=limit)}


@app.api_route(
    "/api/run-now", methods=["POST", "GET"], tags=["actions"], summary="Evaluate now"
)
def run_now() -> dict:
    """Trigger an evaluation immediately instead of waiting for the hourly job.

    Digests everything currently buffered, sends it to Claude, stores the
    finding, and purges the evaluated raw logs. Useful for testing.

    Accepts **GET or POST** for convenience (so a plain `curl`/browser works).
    Returns `{ "ran": false, "result": null }` if there were no logs to
    evaluate or the model call failed (raw logs are kept on failure), otherwise
    `{ "ran": true, "result": { ...the structured AI result... } }`.
    """
    result = evaluator.run_evaluation()
    return {"ran": result is not None, "result": result}


@app.get("/api/weekly", tags=["monitoring"], summary="Latest weekly pattern review")
def weekly() -> JSONResponse:
    """Return the most recent weekly pattern review, or `null` if none exist.

    Shape:
    ```json
    {
      "id": 4, "ts": 1750896000.0,
      "period_start": 1750291200.0, "period_end": 1750896000.0,
      "window_days": 7, "finding_count": 142,
      "payload": {
        "period_status": "action",
        "overall_assessment": "...",
        "recurring_issues": [ {"title": "...", "category": "...",
          "severity": "critical", "days_seen": 7, "frequency": "nightly",
          "detail": "...", "recommendation": "..."} ],
        "trends": [ {"title": "...", "direction": "increasing", "detail": "..."} ],
        "watchlist": ["..."]
      }
    }
    ```
    """
    return JSONResponse(db.latest_weekly())


@app.get("/api/weekly/history", tags=["monitoring"], summary="Weekly review history")
def weekly_history(limit: int = 30) -> dict:
    """Return recent weekly pattern reviews, newest first (default 30)."""
    return {"weekly": db.list_weekly(limit=limit)}


@app.api_route(
    "/api/run-weekly", methods=["POST", "GET"], tags=["actions"],
    summary="Run weekly review now",
)
def run_weekly() -> dict:
    """Run the weekly pattern review immediately (rolling `WEEKLY_WINDOW_DAYS`).

    Accepts GET or POST. Returns `{ "ran": false, "result": null }` if there
    were no findings in the window or the model call failed, otherwise
    `{ "ran": true, "result": { ...the structured review... } }`.
    """
    result = evaluator.run_weekly_review()
    return {"ran": result is not None, "result": result}


@app.get("/", tags=["ui"], summary="Dashboard", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


# Serve any other static assets if added later.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
