"""Digest construction — the cost-control layer."""
from __future__ import annotations

import time

from app import db
from app.preprocess import build_digest, templatize


def test_templatize_masks_volatile_tokens():
    masked = templatize("conn from 192.168.0.44:51234 mac aa:bb:cc:dd:ee:ff id 0xDEAD")
    assert "<IP>" in masked and "<MAC>" in masked and "<HEX>" in masked
    assert "192.168.0.44" not in masked


def test_templatize_collapses_repetitive_lines_to_one_template():
    """The whole point: N near-identical firewall lines become one pattern."""
    a = templatize("DROP src=10.0.0.1 spt=443")
    b = templatize("DROP src=10.0.0.9 spt=8080")
    assert a == b


def test_templatize_also_destroys_measurements():
    """Documented limitation, not a bug — it is why metrics bypass this path.

    See fleet-health-design.md: `\\b\\d+\\b -> <N>` masks the numbers that *are*
    the payload for vitals, so vitals must not go through the digest.
    """
    masked = templatize("temp=39.4 load=0.11 disk_pct=14")
    assert "39.4" not in masked and "<N>" in masked


def _rows(insert_log_at, n: int, message: str, severity: int = 6):
    now = time.time()
    for i in range(n):
        insert_log_at(now - 100 + i, message, severity=severity)
    return db.fetch_logs_until(now + 1)


def test_digest_counts_and_groups(insert_log_at):
    rows = _rows(insert_log_at, 7, "DROP src=10.0.0.1 spt=443")
    text, stats = build_digest(rows)

    assert stats["total_lines"] == 7
    assert stats["distinct_patterns"] == 1
    assert "Total syslog lines this period: 7" in text


def test_digest_keeps_elevated_lines_verbatim(insert_log_at):
    """Elevated-severity samples keep real values — that is where evidence lives."""
    rows = _rows(insert_log_at, 1, "kernel: Under-voltage detected on 192.168.0.44",
                 severity=3)
    text, stats = build_digest(rows)

    assert stats["elevated_count"] == 1
    assert "Under-voltage detected" in text


def test_digest_reports_source_hosts(insert_log_at):
    rows = _rows(insert_log_at, 3, "something happened")
    text, _ = build_digest(rows)
    assert "Source hosts:" in text and "testhost" in text
