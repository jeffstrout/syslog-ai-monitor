"""Entrypoint: start the syslog listener, the scheduler, and the web server.

Everything runs in one asyncio process. The syslog UDP/TCP servers and the
Uvicorn web server share the event loop; APScheduler runs the hourly evaluation
and the nightly retention purge.
"""
from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db, syslog_listener
from .config import settings
from .evaluator import purge_old_findings, run_evaluation, run_weekly_review
from .web import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("main")


def _eval_trigger(minutes: int):
    """Build a wall-clock-aligned trigger for the evaluation job.

    Aligns to the top of the hour where possible so runs land on clean times
    (e.g. 9:00, 10:00) rather than drifting from container-start time:
      - 60 (or any multiple of 60) -> top of the hour, every N hours
      - a divisor of 60 (30, 20, 15, 10, 5, ...) -> aligned marks each hour
      - anything else -> plain interval from start (can't align cleanly)
    Returns (trigger, human_description).
    """
    if minutes % 60 == 0:
        hours = minutes // 60
        return (
            CronTrigger(minute=0, hour="*" if hours == 1 else f"*/{hours}"),
            f"at :00 of every {hours} hour(s)",
        )
    if 60 % minutes == 0:
        return (
            CronTrigger(minute=f"*/{minutes}"),
            f"every {minutes} min, aligned to the hour",
        )
    return (IntervalTrigger(minutes=minutes), f"every {minutes} min from start")


async def main() -> None:
    db.init()
    log.info("database ready at %s", settings.db_path)

    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is not set — evaluations will fail")

    # APScheduler: top-of-hour evaluation + nightly retention purge.
    # Use an explicit timezone so "the top of the hour" means local time, not
    # the container's default (UTC). Cron schedules are wall-clock aligned.
    tz = ZoneInfo(settings.timezone) if settings.timezone else None
    scheduler = AsyncIOScheduler(timezone=tz) if tz else AsyncIOScheduler()

    # Schedule the functions directly. APScheduler's AsyncIOExecutor runs sync
    # job functions in a thread pool, which is exactly what we want for the
    # blocking Anthropic call + SQLite writes — it keeps them off the event loop
    # that serves syslog + web. (Do NOT wrap these in a lambda that calls
    # asyncio.get_event_loop(): the job runs in a worker thread with no running
    # loop, so that raises and the evaluation silently never happens.)
    eval_trigger, eval_desc = _eval_trigger(settings.eval_interval_minutes)
    scheduler.add_job(
        run_evaluation, eval_trigger,
        id="hourly_eval", max_instances=1, coalesce=True,
    )
    # Weekly pattern review — daily at WEEKLY_REVIEW_HOUR over a rolling window.
    scheduler.add_job(
        run_weekly_review,
        CronTrigger(hour=settings.weekly_review_hour, minute=0),
        id="weekly_review", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        purge_old_findings,
        CronTrigger(hour=3, minute=30), id="retention_purge",
    )
    scheduler.start()

    job = scheduler.get_job("hourly_eval")
    wk = scheduler.get_job("weekly_review")
    log.info("scheduler started: evaluate %s (next run %s), retention %d days, tz=%s",
             eval_desc, job.next_run_time, settings.retention_days,
             settings.timezone or "system-local")
    log.info("weekly pattern review: %d-day window, daily at %02d:00 (next run %s)",
             settings.weekly_window_days, settings.weekly_review_hour,
             wk.next_run_time)

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
