"""Entrypoint: start the syslog listener, the scheduler, and the web server.

Everything runs in one asyncio process. The syslog UDP/TCP servers and the
Uvicorn web server share the event loop; APScheduler runs the hourly evaluation
and the nightly retention purge.
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, syslog_listener
from .config import settings
from .evaluator import purge_old_findings, run_evaluation
from .web import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def main() -> None:
    db.init()
    log.info("database ready at %s", settings.db_path)

    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is not set — evaluations will fail")

    # APScheduler: hourly evaluation + nightly retention purge.
    # Evaluation runs in a thread so the blocking Anthropic call and SQLite
    # writes don't stall the event loop serving syslog + web.
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.get_event_loop().run_in_executor(None, run_evaluation),
        "interval",
        minutes=settings.eval_interval_minutes,
        id="hourly_eval",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: asyncio.get_event_loop().run_in_executor(None, purge_old_findings),
        "cron", hour=3, minute=30, id="retention_purge",
    )
    scheduler.start()
    log.info("scheduler started: evaluate every %d min, retention %d days",
             settings.eval_interval_minutes, settings.retention_days)

    # Web server (Uvicorn) as an asyncio task on this same loop.
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.web_port,
        log_level="info", access_log=False,
    )
    server = uvicorn.Server(config)

    # Run the syslog listener and the web server together for the loop's lifetime.
    await asyncio.gather(
        syslog_listener.start(settings.syslog_port),
        server.serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
