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

    # Safety bounds on the raw-log buffer. run_evaluation deliberately KEEPS raw
    # logs when a model call fails, so the window isn't lost -- but that must be
    # bounded. With no ceiling, a sustained API failure grew this table to 15.9M
    # rows / 5 GB over 8.9 days; every run then spent ~41 minutes holding the DB
    # lock, browning out the dashboard for most of each hour.
    eval_max_rows: int = _int("EVAL_MAX_ROWS", 500_000)
    raw_log_max_age_hours: int = _int("RAW_LOG_MAX_AGE_HOURS", 24)
    alert_failure_cooldown_hours: int = _int("ALERT_FAILURE_COOLDOWN_HOURS", 6)

    # Circuit breaker. A permanent model error (billing, auth) cannot clear on
    # retry, so re-fetching and re-templatizing the whole buffer every hour burns
    # CPU on work that is guaranteed to fail — and that work is pure-Python regex
    # in a worker thread, so it starves the event loop while it runs. Back off
    # instead, doubling from the base up to the cap. Any success resets it.
    breaker_backoff_base_minutes: int = _int("BREAKER_BACKOFF_BASE_MINUTES", 60)
    breaker_backoff_max_hours: int = _int("BREAKER_BACKOFF_MAX_HOURS", 6)

    # Email
    smtp_host: str = os.getenv("SMTP_HOST", "").strip()
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "").strip()
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    alert_from: str = os.getenv("ALERT_FROM", "syslog-pi@example.com").strip()
    alert_to: str = os.getenv("ALERT_TO", "").strip()
    alert_min_severity: str = os.getenv("ALERT_MIN_SEVERITY", "error").strip().lower()

    # MQTT / Home Assistant. Disabled unless MQTT_HOST is set, mirroring the SMTP
    # handling. Topic scheme matches ac-monitor (homelab-standards docs/mqtt.md);
    # HA entities are bound to that shape, so it must not drift.
    mqtt_host: str = os.getenv("MQTT_HOST", "").strip()
    mqtt_port: int = _int("MQTT_PORT", 1883)
    mqtt_user: str = os.getenv("MQTT_USER", "").strip()
    mqtt_pass: str = os.getenv("MQTT_PASS", "")
    mqtt_base_topic: str = os.getenv("MQTT_BASE_TOPIC", "syslog_monitor").strip()
    mqtt_discovery_prefix: str = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant").strip()
    mqtt_publish_interval_minutes: int = _int("MQTT_PUBLISH_INTERVAL_MINUTES", 1)

    # Storage
    db_path: str = os.getenv("DB_PATH", "/data/syslog.db").strip()

    # Timezone for scheduling (so evaluations land on the local top-of-hour).
    # IANA name, e.g. "America/Chicago". Empty = container/system local time.
    timezone: str = os.getenv("TZ", "").strip()

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.alert_to)

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self.mqtt_host)


settings = Settings()


def severity_rank(sev: str) -> int:
    """Return the ladder index of a severity name (unknown -> 0)."""
    try:
        return SEVERITY_ORDER.index((sev or "info").lower())
    except ValueError:
        return 0
