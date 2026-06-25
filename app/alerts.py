"""Email alerting via SMTP when an evaluation surfaces notable findings."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import settings, severity_rank

log = logging.getLogger("alerts")


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


def maybe_send(result: dict) -> None:
    """Send an email if any finding meets the configured severity threshold."""
    if not _should_alert(result):
        return

    msg = EmailMessage()
    status = result.get("overall_status", "alert").upper()
    msg["Subject"] = f"[Syslog Monitor] {status}: {result.get('summary', '')[:80]}"
    msg["From"] = settings.alert_from
    msg["To"] = settings.alert_to
    msg.set_content(_format_body(result))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_pass)
            smtp.send_message(msg)
        log.info("alert email sent to %s", settings.alert_to)
    except Exception:
        log.exception("failed to send alert email")
