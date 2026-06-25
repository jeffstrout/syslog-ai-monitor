"""Claude Haiku evaluation of a log digest using structured JSON output."""
from __future__ import annotations

import json
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
    "and abnormal spikes. Ignore routine, benign chatter (normal firewall allow/deny "
    "noise, periodic status messages, expected client connect/disconnect).\n\n"
    "Return a concise structured assessment. Set overall_status to 'error' if "
    "anything needs attention now, 'warning' for things worth watching, 'ok' if the "
    "hour looks healthy. Each finding must be actionable and reference concrete "
    "evidence from the digest. If nothing is wrong, return an empty findings list."
)


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
        output_config={
            "format": {"type": "json_schema", "schema": RESULT_SCHEMA}
        },
    )

    # With output_config.format the first text block is guaranteed valid JSON.
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    result = json.loads(text)
    log.info(
        "evaluation complete: status=%s findings=%d (lines=%s)",
        result.get("overall_status"), len(result.get("findings", [])),
        stats.get("total_lines"),
    )
    return result
