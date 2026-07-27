"""Email alerting via SMTP when an evaluation surfaces notable findings."""
from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage

from .config import settings, severity_rank

log = logging.getLogger("alerts")

# Throttles the evaluation-failure email: a persistent outage (expired credits,
# revoked key) would otherwise send one every hour for days.
_last_failure_alert: float = 0.0


def _should_alert(result: dict) -> bool:
    if not settings.email_enabled:
        return False
    threshold = severity_rank(settings.alert_min_severity)
    return any(
        severity_rank(f.get("severity", "info")) >= threshold
        for f in result.get("findings", [])
    )


def _format_body(result: dict) -> str:
    lines = [
        f"Overall status: {result.get('overall_status', '?').upper()}",
        f"Summary: {result.get('summary', '')}",
        "",
        "Findings:",
    ]
    threshold = severity_rank(settings.alert_min_severity)
    for f in result.get("findings", []):
        if severity_rank(f.get("severity", "info")) < threshold:
            continue
        lines += [
            f"  [{f.get('severity', '?').upper()}] {f.get('title', '')} "
            f"({f.get('category', '')}, x{f.get('occurrences', 0)})",
            f"    {f.get('detail', '')}",
            f"    Evidence: {f.get('evidence', '')}",
            f"    Recommendation: {f.get('recommendation', '')}",
            "",
        ]
    return "\n".join(lines)


def _send(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.alert_from
    msg["To"] = settings.alert_to
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_pass)
        smtp.send_message(msg)


def maybe_send_failure(exc: Exception, permanent: bool, buffered: int) -> None:
    """Email when the evaluation itself fails, not just when it finds problems.

    maybe_send() runs only after a *successful* evaluation, so the model call
    breaking — the failure that matters most — is otherwise silent by
    construction. Throttled to one message per ALERT_FAILURE_COOLDOWN_HOURS.
    """
    global _last_failure_alert
    if not settings.email_enabled:
        return

    now = time.time()
    if now - _last_failure_alert < settings.alert_failure_cooldown_hours * 3600:
        return
    _last_failure_alert = now

    kind = "PERMANENT" if permanent else "transient"
    body = "\n".join([
        f"The hourly evaluation failed with a {kind} error.",
        "",
        f"Error: {type(exc).__name__}: {exc}",
        "",
        f"Raw log lines currently buffered: {buffered:,}",
        f"Buffer is capped at {settings.raw_log_max_age_hours}h "
        f"(RAW_LOG_MAX_AGE_HOURS); older unevaluated lines are dropped.",
        "",
        "A permanent error (billing, auth, bad request) will not clear on retry "
        "and needs attention." if permanent else
        "A transient error should clear on the next scheduled run.",
    ])

    try:
        _send(f"[Syslog Monitor] evaluation FAILED ({kind})", body)
        log.info("evaluation-failure alert sent to %s", settings.alert_to)
    except Exception:
        log.exception("failed to send evaluation-failure email")


def maybe_send(result: dict) -> None:
    """Send an email if any finding meets the configured severity threshold."""
    if not _should_alert(result):
        return

    status = result.get("overall_status", "alert").upper()
    try:
        _send(f"[Syslog Monitor] {status}: {result.get('summary', '')[:80]}",
              _format_body(result))
        log.info("alert email sent to %s", settings.alert_to)
    except Exception:
        log.exception("failed to send alert email")
