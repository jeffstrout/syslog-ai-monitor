"""Environment-driven settings for the Syslog AI Monitor."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Ordered severity ladder used for comparisons (alerts, sorting).
SEVERITY_ORDER = ["info", "warning", "error", "critical"]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    model: str = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001").strip()

    # Schedule / retention
    eval_interval_minutes: int = _int("EVAL_INTERVAL_MINUTES", 60)
    retention_days: int = _int("RETENTION_DAYS", 30)

    # Weekly pattern review: roll up the last N days of hourly findings to spot
    # recurring/trending issues. Runs daily over a rolling window so it stays
    # current. weekly_review_hour is the local hour-of-day (0-23) it runs at.
    weekly_window_days: int = _int("WEEKLY_WINDOW_DAYS", 7)
    weekly_review_hour: int = _int("WEEKLY_REVIEW_HOUR", 6)

    # Ports
    syslog_port: int = _int("SYSLOG_PORT", 514)
    web_port: int = _int("WEB_PORT", 8080)

    # Digest limits
    digest_max_templates: int = _int("DIGEST_MAX_TEMPLATES", 60)
    digest_max_samples: int = _int("DIGEST_MAX_SAMPLES", 200)
    digest_max_chars: int = _int("DIGEST_MAX_CHARS", 24000)

    # Email
    smtp_host: str = os.getenv("SMTP_HOST", "").strip()
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "").strip()
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    alert_from: str = os.getenv("ALERT_FROM", "syslog-pi@example.com").strip()
    alert_to: str = os.getenv("ALERT_TO", "").strip()
    alert_min_severity: str = os.getenv("ALERT_MIN_SEVERITY", "error").strip().lower()

    # Storage
    db_path: str = os.getenv("DB_PATH", "/data/syslog.db").strip()

    # Timezone for scheduling (so evaluations land on the local top-of-hour).
    # IANA name, e.g. "America/Chicago". Empty = container/system local time.
    timezone: str = os.getenv("TZ", "").strip()

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.alert_to)


settings = Settings()


def severity_rank(sev: str) -> int:
    """Return the ladder index of a severity name (unknown -> 0)."""
    try:
        return SEVERITY_ORDER.index((sev or "info").lower())
    except ValueError:
        return 0
