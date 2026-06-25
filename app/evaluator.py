"""The hourly job: digest the buffered logs, evaluate with Claude, store, purge."""
from __future__ import annotations

import logging
import time

from . import alerts, claude_client, db
from .config import settings
from .preprocess import build_digest

log = logging.getLogger("evaluator")


def run_evaluation() -> dict | None:
    """Evaluate everything buffered up to now. Returns the result, or None if empty."""
    cutoff = time.time()
    rows = db.fetch_logs_until(cutoff)

    if not rows:
        log.info("no logs to evaluate this period")
        return None

    digest_text, stats = build_digest(rows)
    log.info("evaluating %d lines (%d patterns)",
             stats["total_lines"], stats["distinct_patterns"])

    try:
        result = claude_client.evaluate(digest_text, stats)
    except Exception:
        log.exception("Claude evaluation failed; keeping raw logs for next run")
        return None  # leave raw logs in place so the data isn't lost

    db.insert_finding(
        overall_status=result.get("overall_status", "ok"),
        summary=result.get("summary", ""),
        log_count=stats["total_lines"],
        payload=result,
    )

    # Evaluation succeeded and is persisted — drop the raw logs we just processed.
    deleted = db.delete_logs_until(cutoff)
    log.info("stored finding and purged %d raw logs", deleted)

    alerts.maybe_send(result)
    return result


def purge_old_findings() -> None:
    """Delete findings older than the retention window (nightly job)."""
    cutoff = time.time() - settings.retention_days * 86400
    removed = db.purge_findings(cutoff)
    if removed:
        log.info("retention purge removed %d findings", removed)
