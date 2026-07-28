"""The hourly job: digest the buffered logs, evaluate with Claude, store, purge.

Also the weekly pattern review: roll up the last N days of hourly findings and
ask Claude to surface recurring issues and trends.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from . import alerts, claude_client, db, mqtt_out
from .config import settings
from .preprocess import build_digest

log = logging.getLogger("evaluator")

# Cap how many hourly summaries we inline into the weekly digest (the aggregate
# pattern table carries the recurrence signal regardless).
_WEEKLY_MAX_SUMMARIES = 200


def _local_dt(ts: float) -> datetime:
    tz = ZoneInfo(settings.timezone) if settings.timezone else None
    return datetime.fromtimestamp(ts, tz)


# Circuit-breaker state. Module-level, so it resets on restart — deliberate: a
# restart is a reasonable moment to re-probe, and persisting it would mean a
# stale breaker could outlive the problem.
_breaker_open_until: float = 0.0
_breaker_consecutive: int = 0


def _breaker_backoff_seconds(consecutive: int) -> float:
    """Double from the base up to the cap: 1h, 2h, 4h, 6h, 6h, …"""
    base = settings.breaker_backoff_base_minutes * 60
    cap = settings.breaker_backoff_max_hours * 3600
    return float(min(base * (2 ** max(0, consecutive - 1)), cap))


def _trip_breaker() -> float:
    """Open the breaker after a permanent failure; returns the delay in seconds."""
    global _breaker_open_until, _breaker_consecutive
    _breaker_consecutive += 1
    delay = _breaker_backoff_seconds(_breaker_consecutive)
    _breaker_open_until = time.time() + delay
    return delay


def _reset_breaker() -> None:
    """Any success clears the breaker, so recovery needs no intervention."""
    global _breaker_open_until, _breaker_consecutive
    _breaker_open_until = 0.0
    _breaker_consecutive = 0


def breaker_state() -> dict:
    """Reported by /api/health so an operator can tell a breaker from a hang."""
    remaining = _breaker_open_until - time.time()
    is_open = remaining > 0
    return {
        "open": is_open,
        "retry_in_seconds": round(remaining) if is_open else None,
        "consecutive_permanent_failures": _breaker_consecutive,
    }


def publish_mqtt() -> None:
    """Push current state to MQTT. Also the scheduled heartbeat job.

    Lives here rather than in mqtt_out so that module stays free of `evaluator`
    (which imports it) — the breaker state is passed in instead.
    """
    mqtt_out.publisher.publish(breaker_state())


def _is_permanent_api_error(exc: Exception) -> bool:
    """True for model errors that retrying on the next tick cannot fix.

    A 400 "credit balance is too low" or a 401 fails identically every hour, so
    treating it as a transient blip means retrying forever while the raw-log
    buffer grows without bound. 429 (rate limit) is excluded — that one *is*
    worth retrying.
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 429


def _enforce_raw_log_bound() -> None:
    """Drop raw logs older than the hard bound, whatever the evaluation outcome.

    run_evaluation deliberately keeps raw logs when the model call fails so the
    window isn't lost — but unbounded, that is how 8.9 days of failures became
    15.9M rows / 5 GB. This caps the damage at RAW_LOG_MAX_AGE_HOURS, trading the
    oldest unevaluated logs for the service staying responsive.
    """
    hours = settings.raw_log_max_age_hours
    if hours <= 0:
        return
    removed = db.delete_logs_until(time.time() - hours * 3600)
    if removed:
        log.warning(
            "raw-log bound: dropped %d unevaluated rows older than %dh",
            removed, hours,
        )


def run_evaluation(force: bool = False) -> dict | None:
    """Evaluate everything buffered up to now. Returns the result, or None if empty.

    `force=True` bypasses the circuit breaker — an explicit manual trigger
    (`/api/run-now`) is a deliberate probe and should always attempt the call.
    """
    cutoff = time.time()

    # Skip *before* fetching and digesting: those are the expensive parts, and
    # while the breaker is open they are guaranteed to be wasted.
    if not force and _breaker_open_until > cutoff:
        log.info(
            "circuit breaker open for another %d min after %d permanent "
            "failure(s) — skipping fetch and digest",
            round((_breaker_open_until - cutoff) / 60), _breaker_consecutive,
        )
        _enforce_raw_log_bound()
        return None

    rows = db.fetch_logs_until(cutoff, limit=settings.eval_max_rows)

    if not rows:
        log.info("no logs to evaluate this period")
        _enforce_raw_log_bound()
        return None

    # Delete by id, not by `cutoff`: when the batch is capped, rows newer than the
    # last one evaluated are still buffered and must survive.
    batch_max_id = max(r["id"] for r in rows)

    digest_text, stats = build_digest(rows)
    log.info("evaluating %d lines (%d patterns)",
             stats["total_lines"], stats["distinct_patterns"])

    try:
        result = claude_client.evaluate(digest_text, stats)
    except Exception as exc:
        permanent = _is_permanent_api_error(exc)
        log.exception(
            "Claude evaluation failed (%s); keeping raw logs for next run",
            "permanent — will not resolve on retry" if permanent else "transient",
        )
        if permanent:
            # Transient errors deliberately do NOT trip the breaker — a 429 or a
            # 500 should simply retry on the next tick.
            delay = _trip_breaker()
            log.warning(
                "circuit breaker open for %d min; evaluations will be skipped "
                "until then (use /api/run-now to force an attempt)",
                round(delay / 60),
            )
        # maybe_send() only fires on a *successful* evaluation, so without this the
        # failure of the model call itself would be entirely silent.
        alerts.maybe_send_failure(exc, permanent=permanent,
                                  buffered=db.raw_log_count())
        _enforce_raw_log_bound()
        publish_mqtt()   # surface the breaker/staleness in HA immediately
        return None  # leave raw logs in place so the data isn't lost

    # The call worked, so whatever was wrong has cleared — recovery is automatic.
    _reset_breaker()

    db.insert_finding(
        overall_status=result.get("overall_status", "ok"),
        summary=result.get("summary", ""),
        log_count=stats["total_lines"],
        payload=result,
    )

    # Evaluation succeeded and is persisted — drop the raw logs we just processed.
    deleted = db.delete_logs_through_id(batch_max_id)
    log.info("stored finding and purged %d raw logs", deleted)
    _enforce_raw_log_bound()

    alerts.maybe_send(result)
    publish_mqtt()
    return result


def purge_old_findings() -> None:
    """Delete findings older than the retention window (nightly job)."""
    cutoff = time.time() - settings.retention_days * 86400
    removed = db.purge_findings(cutoff)
    removed_weekly = db.purge_weekly(cutoff)
    if removed or removed_weekly:
        log.info("retention purge removed %d findings, %d weekly summaries",
                 removed, removed_weekly)


def _build_weekly_digest(findings: list[dict]) -> tuple[str, dict]:
    """Turn the period's hourly findings into a compact rollup for the model.

    Combines (a) status distribution, (b) an aggregated table of every distinct
    finding title with how many days/hours it appeared and total occurrences —
    this is the recurrence signal — and (c) a capped chronological list of the
    hourly summaries for narrative context.
    """
    status_counts: Counter[str] = Counter()
    # title -> {hours, occ, days:set, severity, category}
    by_title: dict[str, dict] = {}

    for f in findings:
        status_counts[f["overall_status"]] += 1
        day = _local_dt(f["ts"]).strftime("%Y-%m-%d")
        for issue in f["payload"].get("findings", []):
            title = issue.get("title", "(untitled)")
            agg = by_title.setdefault(title, {
                "hours": 0, "occ": 0, "days": set(),
                "severity": issue.get("severity", "info"),
                "category": issue.get("category", ""),
            })
            agg["hours"] += 1
            agg["occ"] += int(issue.get("occurrences", 0) or 0)
            agg["days"].add(day)

    lines: list[str] = []
    start = _local_dt(findings[0]["ts"]).strftime("%Y-%m-%d %H:%M")
    end = _local_dt(findings[-1]["ts"]).strftime("%Y-%m-%d %H:%M")
    lines.append(f"Hourly evaluations reviewed: {len(findings)} "
                 f"(from {start} to {end})")
    lines.append("Status distribution: " + ", ".join(
        f"{s}={status_counts.get(s, 0)}" for s in ("ok", "warning", "error")))

    lines.append("")
    lines.append("=== Recurring findings across the period "
                 "(title — days seen / hours seen / total occurrences) ===")
    ranked = sorted(by_title.items(),
                    key=lambda kv: (len(kv[1]["days"]), kv[1]["hours"]),
                    reverse=True)
    for title, agg in ranked:
        lines.append(
            f"[{agg['severity']}] {title} ({agg['category']}) — "
            f"{len(agg['days'])} day(s) / {agg['hours']} hour(s) / "
            f"{agg['occ']} occurrences"
        )

    lines.append("")
    sample = findings[-_WEEKLY_MAX_SUMMARIES:]
    lines.append(f"=== Hourly summaries ({len(sample)} of {len(findings)}) ===")
    for f in sample:
        when = _local_dt(f["ts"]).strftime("%m-%d %H:%M")
        lines.append(f"{when} [{f['overall_status']}] {f['summary']}")

    stats = {
        "finding_count": len(findings),
        "status_counts": dict(status_counts),
        "distinct_findings": len(by_title),
    }
    return "\n".join(lines), stats


def run_weekly_review() -> dict | None:
    """Review the last WEEKLY_WINDOW_DAYS of findings for patterns. Returns result."""
    end = time.time()
    start = end - settings.weekly_window_days * 86400
    findings = db.fetch_findings_since(start)

    if not findings:
        log.info("no findings in the last %d days to review",
                 settings.weekly_window_days)
        return None

    digest_text, stats = _build_weekly_digest(findings)
    log.info("weekly review over %d findings (%d distinct issues)",
             stats["finding_count"], stats["distinct_findings"])

    try:
        result = claude_client.review_week(digest_text, stats)
    except Exception:
        log.exception("weekly review failed")
        return None

    db.insert_weekly(
        period_start=findings[0]["ts"],
        period_end=findings[-1]["ts"],
        window_days=settings.weekly_window_days,
        finding_count=stats["finding_count"],
        payload=result,
    )
    log.info("stored weekly review (status=%s)", result.get("period_status"))
    return result
