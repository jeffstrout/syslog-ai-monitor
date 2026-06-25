"""Turn a batch of raw syslog rows into a compact, information-dense digest.

UDM Pro firewall logging can produce tens of thousands of near-identical lines
per hour. Sending them raw to the model would be slow and expensive, so we:

  1. Mask volatile tokens (IPs, MACs, ports, timestamps, numbers, hex) so that
     structurally-identical messages collapse to one "template".
  2. Count occurrences per template and keep the most frequent ones.
  3. Preserve a sample of elevated-severity lines (syslog severity <= 4, i.e.
     warning/error/critical/alert/emergency) with real values intact.
  4. Emit a text digest bounded by hard caps (templates / samples / chars).
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter

from .config import settings

# Volatile-token patterns, applied in order. Order matters: match more specific
# tokens (MAC, IPv6, timestamps) before generic numbers.
_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\b"), "<MAC>"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"), "<IPV6>"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"), "<IP>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<TS>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "<TIME>"),
    (re.compile(r"\b0x[0-9A-Fa-f]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9A-Fa-f]{12,}\b"), "<HEX>"),
    (re.compile(r"\b\d+\b"), "<N>"),
]

# syslog severity: 0 emerg .. 7 debug. <=4 means warning or worse.
ELEVATED_MAX_SEVERITY = 4


def templatize(message: str) -> str:
    out = message
    for pattern, repl in _SUBS:
        out = pattern.sub(repl, out)
    return re.sub(r"\s+", " ", out).strip()


def _severity_name(sev: int | None) -> str:
    names = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
    if sev is None or sev < 0 or sev > 7:
        return "unknown"
    return names[sev]


def build_digest(rows: list[sqlite3.Row]) -> tuple[str, dict]:
    """Return (digest_text, stats). stats includes counts for storage/UI."""
    total = len(rows)
    template_counts: Counter[str] = Counter()
    elevated: list[sqlite3.Row] = []
    hosts: Counter[str] = Counter()

    for r in rows:
        template_counts[templatize(r["message"])] += 1
        if r["host"]:
            hosts[r["host"]] += 1
        sev = r["severity"]
        if sev is not None and sev <= ELEVATED_MAX_SEVERITY:
            elevated.append(r)

    top_templates = template_counts.most_common(settings.digest_max_templates)
    elevated_sample = elevated[: settings.digest_max_samples]

    lines: list[str] = []
    lines.append(f"Total syslog lines this period: {total}")
    lines.append(f"Distinct message patterns: {len(template_counts)}")
    if hosts:
        host_str = ", ".join(f"{h} ({c})" for h, c in hosts.most_common(10))
        lines.append(f"Source hosts: {host_str}")

    lines.append("")
    lines.append("=== Top message patterns (pattern × count; volatile values masked) ===")
    for tmpl, count in top_templates:
        lines.append(f"[{count:>6}×] {tmpl}")

    lines.append("")
    lines.append(
        f"=== Elevated-severity sample (warning or worse), "
        f"{len(elevated_sample)} of {len(elevated)} ==="
    )
    for r in elevated_sample:
        host = r["host"] or "?"
        lines.append(f"({_severity_name(r['severity'])}) {host}: {r['message']}")

    text = "\n".join(lines)
    if len(text) > settings.digest_max_chars:
        text = text[: settings.digest_max_chars] + "\n…[digest truncated]"

    stats = {
        "total_lines": total,
        "distinct_patterns": len(template_counts),
        "elevated_count": len(elevated),
        "top_hosts": hosts.most_common(10),
    }
    return text, stats
