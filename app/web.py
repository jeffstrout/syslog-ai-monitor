"""FastAPI app: JSON API + the static dashboard."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, evaluator

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Syslog AI Monitor")


@app.get("/api/status")
def status() -> dict:
    """Lightweight health/overview for the dashboard header."""
    latest = db.latest_finding()
    return {
        "buffered_logs": db.raw_log_count(),
        "latest": latest,
    }


@app.get("/api/latest")
def latest() -> JSONResponse:
    return JSONResponse(db.latest_finding())


@app.get("/api/history")
def history(limit: int = 200) -> dict:
    return {"findings": db.list_findings(limit=limit)}


@app.post("/api/run-now")
def run_now() -> dict:
    """Trigger an evaluation immediately (debug/manual use)."""
    result = evaluator.run_evaluation()
    return {"ran": result is not None, "result": result}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


# Serve any other static assets if added later.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
