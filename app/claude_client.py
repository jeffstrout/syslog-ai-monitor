"""Claude Haiku evaluation of a log digest using structured (tool-call) output."""
from __future__ import annotations

import logging

import anthropic

from .config import settings

log = logging.getLogger("claude")

# JSON schema the model must fill. Structured outputs guarantee valid JSON
# matching this shape (supported on Haiku 4.5 via output_config.format).
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_status": {"type": "string", "enum": ["ok", "warning", "error"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error", "critical"],
                    },
                    "category": {"type": "string"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string"},
                    "occurrences": {"type": "integer"},
                    "recommendation": {"type": "string"},
                },
                "required": [
                    "severity", "category", "title", "detail",
                    "evidence", "occurrences", "recommendation",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overall_status", "summary", "findings"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a network operations analyst reviewing one hour of syslog output "
    "from a Ubiquiti UniFi UDM Pro router/firewall on a home network. You are "
    "given a pre-aggregated digest: total line counts, the most frequent message "
    "patterns (with volatile values like IPs and ports masked), and a sample of "
    "elevated-severity log lines. Lines may be in CEF format "
    "(CEF:Version|Vendor|Product|Version|SignatureID|Name|Severity|key=value...), "
    "which is how the UDM Pro's SIEM export encodes firewall and threat events.\n\n"
    "Identify genuine problems: hardware faults, service crashes, failed logins or "
    "intrusion attempts, DNS/DHCP failures, WAN/uplink flapping, interface errors, "
    "VPN/tunnel failures, and abnormal spikes.\n\n"
    "IMPORTANT — the UDM Pro and its access points are extremely chatty, and most "
    "elevated-severity lines are routine internal daemon noise, NOT problems. Treat "
    "the following as benign background noise: do NOT create findings for them, and "
    "do NOT let them affect overall_status, unless they recur persistently AND you "
    "can tie them to a concrete functional failure (lost connectivity, a service "
    "that stays down, a real auth/intrusion event):\n"
    "  - systemd 'Got notification message from PID X, but reception only permitted "
    "for main PID' (e.g. udapi-server) — normal process noise\n"
    "  - mca-ctrl / mca-proto 'recvfrom header: No such file or directory', "
    "'Got no response within N seconds', 'service_json event fail, retry' — routine IPC churn\n"
    "  - ubnt-dpi-util 'pcap dumping rate exceeded' / DPI packet skipping — expected under load\n"
    "  - StaTXRetryBurst / wireless TX-retry counts / burst_ratio / rssi telemetry — routine\n"
    "  - garp 'Netlink error response: Resource busy', 'ARP table does not have IP for ...', "
    "wlan objmgr 'peer in L-state' — transient client-state churn\n"
    "  - single/occasional ntpd 'timed out waiting' — transient unless time sync stays broken\n"
    "  - procd / logread SIGTERM->SIGKILL during log rotation or restart — benign\n"
    "  - normal firewall allow/deny flow logging and expected client connect/disconnect\n\n"
    "Genuinely escalate things like: a VPN/OpenVPN client stuck in a cert/auth failure "
    "loop, WAN or uplink down, repeated failed admin logins or intrusion signatures, a "
    "service crash-looping, disk/memory exhaustion, or hardware faults.\n\n"
    "Return a concise structured assessment. Set overall_status to 'error' only if "
    "something genuinely needs attention now, 'warning' for real but non-urgent issues, "
    "and 'ok' if the only elevated lines are the benign noise above. Each finding must "
    "be actionable and reference concrete evidence. Prefer fewer, higher-confidence "
    "findings over flagging everything. If nothing real is wrong, return an empty "
    "findings list and overall_status 'ok'."
)


# Structured output via a forced tool call — portable across anthropic SDK
# versions (works on the pinned SDK, unlike the newer output_config.format).
REPORT_TOOL = {
    "name": "report_assessment",
    "description": "Report the structured assessment of this syslog period.",
    "input_schema": RESULT_SCHEMA,
}


def evaluate(digest_text: str, stats: dict) -> dict:
    """Send the digest to Haiku and return the validated result dict."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    user_content = (
        f"Here is the syslog digest for the last evaluation period.\n\n"
        f"{digest_text}"
    )

    response = client.messages.create(
        model=settings.model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        tools=[REPORT_TOOL],
        tool_choice={"type": "tool", "name": "report_assessment"},
    )

    # Forcing tool_choice guarantees a tool_use block whose .input is the
    # schema-validated result (already a dict — no JSON parsing needed).
    result = next(
        (b.input for b in response.content
         if b.type == "tool_use" and b.name == "report_assessment"),
        None,
    )
    if result is None:
        raise RuntimeError("model did not return the report_assessment tool call")

    log.info(
        "evaluation complete: status=%s findings=%d (lines=%s)",
        result.get("overall_status"), len(result.get("findings", [])),
        stats.get("total_lines"),
    )
    return result


# ── Weekly pattern review ───────────────────────────────────────────────────

WEEKLY_SCHEMA = {
    "type": "object",
    "properties": {
        "period_status": {"type": "string", "enum": ["ok", "watch", "action"]},
        "overall_assessment": {"type": "string"},
        "recurring_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error", "critical"],
                    },
                    "days_seen": {"type": "integer"},
                    "frequency": {"type": "string"},
                    "detail": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
                "required": [
                    "title", "category", "severity", "days_seen",
                    "frequency", "detail", "recommendation",
                ],
                "additionalProperties": False,
            },
        },
        "trends": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["new", "increasing", "steady", "decreasing", "resolved"],
                    },
                    "detail": {"type": "string"},
                },
                "required": ["title", "direction", "detail"],
                "additionalProperties": False,
            },
        },
        "watchlist": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "period_status", "overall_assessment",
        "recurring_issues", "trends", "watchlist",
    ],
    "additionalProperties": False,
}

WEEKLY_TOOL = {
    "name": "report_weekly_review",
    "description": "Report the multi-day pattern review of the syslog findings.",
    "input_schema": WEEKLY_SCHEMA,
}

WEEKLY_SYSTEM_PROMPT = (
    "You are a network operations analyst reviewing several days of hourly "
    "assessments of a home network's UniFi UDM Pro. Each hour, an AI already "
    "summarized that hour's syslog and listed any findings; you are now given a "
    "rollup of those hourly summaries and findings across the whole period.\n\n"
    "Your job is to find PATTERNS that a single hour can't reveal:\n"
    "  - Recurring issues: the same problem appearing across many hours or days "
    "(e.g. a VPN that fails every evening, DPI socket errors several times a "
    "day). Note how many distinct days it was seen and roughly how often.\n"
    "  - Trends: something new this period, getting more frequent, steady, "
    "improving, or resolved.\n"
    "  - A short watchlist: a few concrete things worth keeping an eye on.\n\n"
    "Focus on what is actionable or worth attention. Do NOT re-report one-off, "
    "benign, or routine items that appeared in a single hour and never recurred "
    "— those are noise at this altitude. A genuine recurring failure (like a VPN "
    "certificate loop) matters even if each hour individually looked minor.\n\n"
    "Set period_status to 'action' if something needs attention this week, "
    "'watch' for things worth monitoring, and 'ok' if the period was healthy "
    "with only routine noise. Keep the overall_assessment to 2-4 sentences. If "
    "there are no real patterns, return empty recurring_issues/trends and 'ok'."
)


def review_week(digest_text: str, stats: dict) -> dict:
    """Send the multi-day findings rollup to Haiku and return the result dict."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    user_content = (
        f"Here is the rollup of hourly assessments for the review period.\n\n"
        f"{digest_text}"
    )

    response = client.messages.create(
        model=settings.model,
        max_tokens=4096,
        system=WEEKLY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        tools=[WEEKLY_TOOL],
        tool_choice={"type": "tool", "name": "report_weekly_review"},
    )

    result = next(
        (b.input for b in response.content
         if b.type == "tool_use" and b.name == "report_weekly_review"),
        None,
    )
    if result is None:
        raise RuntimeError("model did not return the report_weekly_review tool call")

    log.info(
        "weekly review complete: status=%s recurring=%d trends=%d (findings=%s)",
        result.get("period_status"), len(result.get("recurring_issues", [])),
        len(result.get("trends", [])), stats.get("finding_count"),
    )
    return result
