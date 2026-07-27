"""MQTT publishing + Home Assistant discovery, with availability via LWT.

Mirrors ``ac-monitor/ac_monitor/mqtt_out.py`` — same topic scheme and payload
shapes, because Home Assistant entities are already bound to them (see
homelab-standards docs/mqtt.md). The message builders are pure (they return
``(topic, payload, retain)`` tuples) so they are testable with no broker;
:class:`MqttPublisher` drives a paho client, injectable for tests.

Why this service publishes availability at all: syslog is push-only, so a healthy
quiet host and a dead host look identical. An MQTT Last Will makes the broker
announce ``offline`` on this box's behalf when its connection drops, which turns
*absence* into the signal. Ping cannot do this — ICMP is answered by the kernel,
so a host whose userspace is entirely wedged still replies (which is exactly what
happened here on 2026-07-27).

This is the manager node, so it is also the box least able to report its own
death: nothing else watches it.
"""
from __future__ import annotations

import json
import logging
import time

from . import db
from .config import settings

log = logging.getLogger("mqtt")

_UID = "syslog_monitor"


def _device() -> dict:
    return {
        "identifiers": [f"{_UID}_pi"],
        "name": "Syslog Monitor",
        "manufacturer": "DIY",
        "model": "RPi 4B — syslog-ai-monitor",
    }


def availability_topic() -> str:
    return f"{settings.mqtt_base_topic}/status"


# --- pure message builders ---------------------------------------------------

def discovery_messages() -> list[tuple[str, str, bool]]:
    """(topic, json_payload, retain) discovery configs — published once, retained."""
    base = settings.mqtt_base_topic
    pre = settings.mqtt_discovery_prefix
    avail = availability_topic()
    out: list[tuple[str, str, bool]] = []

    def entity(component: str, key: str, name: str, topic: str, **extra) -> None:
        payload = {
            "name": name,
            "unique_id": f"{_UID}_{key}",
            "state_topic": topic,
            "availability_topic": avail,
            "device": _device(),
            **extra,
        }
        out.append((f"{pre}/{component}/{_UID}/{key}/config", json.dumps(payload), True))

    def sensor(key, name, topic, **extra):
        entity("sensor", key, name, topic, **extra)

    def binary(key, name, topic, **extra):
        entity("binary_sensor", key, name, topic,
               payload_on="ON", payload_off="OFF", **extra)

    sensor("overall_status", "Overall Status", f"{base}/status/overall")
    sensor("buffered_logs", "Buffered Logs", f"{base}/counts/buffered_logs",
           unit_of_measurement="lines", state_class="measurement")
    sensor("findings_stored", "Findings Stored", f"{base}/counts/findings",
           unit_of_measurement="findings", state_class="measurement")
    sensor("last_evaluation", "Last Evaluation", f"{base}/evaluation/last",
           device_class="timestamp")

    # device_class=problem inverts the sense: ON means "there is a problem".
    binary("evaluation_stale", "Evaluation Stale", f"{base}/evaluation/stale",
           device_class="problem")
    binary("circuit_breaker", "Circuit Breaker Open", f"{base}/evaluation/breaker_open",
           device_class="problem")
    return out


def build_snapshot(breaker: dict) -> dict:
    """Current state, as the publisher and the tests both see it.

    `breaker` is passed in rather than imported so this module stays free of
    `evaluator` — which imports this one.
    """
    latest = db.latest_finding()
    now = time.time()
    last_ts = latest["ts"] if latest else None
    age = (now - last_ts) if last_ts else None
    stale_after = settings.eval_interval_minutes * 60 * 3

    return {
        "overall_status": latest["overall_status"] if latest else "unknown",
        "buffered_logs": db.raw_log_count(),
        "findings_stored": db.findings_count(),
        "last_evaluation_ts": last_ts,
        # Stale, not "healthy": device_class=problem reads ON as the problem.
        "evaluation_stale": age is None or age >= stale_after,
        "breaker_open": bool(breaker.get("open")),
    }


def state_messages(snapshot: dict) -> list[tuple[str, str, bool]]:
    """(topic, payload, retain) live state messages for one publish tick."""
    base = settings.mqtt_base_topic
    out: list[tuple[str, str, bool]] = [
        (f"{base}/status/overall", str(snapshot["overall_status"]), False),
        (f"{base}/counts/buffered_logs", str(snapshot["buffered_logs"]), False),
        (f"{base}/counts/findings", str(snapshot["findings_stored"]), False),
        (f"{base}/evaluation/stale", "ON" if snapshot["evaluation_stale"] else "OFF", False),
        (f"{base}/evaluation/breaker_open", "ON" if snapshot["breaker_open"] else "OFF", False),
    ]
    ts = snapshot.get("last_evaluation_ts")
    if ts:
        # HA's timestamp device_class requires ISO 8601 with an offset.
        iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))
        out.append((f"{base}/evaluation/last", iso, False))
    return out


# --- publisher ---------------------------------------------------------------

def _default_client():  # pragma: no cover - needs paho + a broker
    import paho.mqtt.client as mqtt

    try:   # paho-mqtt 2.x requires an explicit callback API version
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:  # paho-mqtt 1.x
        return mqtt.Client()


class MqttPublisher:
    """Connect, publish, disconnect. No-ops entirely when MQTT is unconfigured.

    ``client_factory`` returns a paho-like client (``will_set``,
    ``username_pw_set``, ``connect``, ``loop_start/stop``, ``publish``,
    ``disconnect``); injected in tests so nothing needs a broker.
    """

    def __init__(self, client_factory=_default_client):
        self._factory = client_factory
        self.client = None
        self.connected = False
        self._discovered = False

    def publish(self, breaker: dict) -> None:
        """Publish one tick. Never raises — MQTT must not break evaluation."""
        if not settings.mqtt_enabled:
            return
        try:
            if self.client is None:
                self._connect()
            if not self.connected:
                return
            if not self._discovered:
                for topic, payload, retain in discovery_messages():
                    self.client.publish(topic, payload, retain=retain)
                self._discovered = True
            for topic, payload, retain in state_messages(build_snapshot(breaker)):
                self.client.publish(topic, payload, retain=retain)
        except Exception:
            log.exception("MQTT publish failed; dropping the client so the next "
                          "tick reconnects")
            self._drop()

    def _connect(self) -> None:
        client = self._factory()
        status = availability_topic()
        # Registered BEFORE connect: the broker only honours a will supplied at
        # connection time. This is the whole point of the feature.
        client.will_set(status, "offline", retain=True)
        if settings.mqtt_user:
            client.username_pw_set(settings.mqtt_user, settings.mqtt_pass)
        client.connect(settings.mqtt_host, settings.mqtt_port)
        # paho's own thread — publishing must not block the shared event loop.
        client.loop_start()
        client.publish(status, "online", retain=True)
        self.client = client
        self.connected = True
        self._discovered = False
        log.info("MQTT connected to %s:%d as %s",
                 settings.mqtt_host, settings.mqtt_port, settings.mqtt_base_topic)

    def _drop(self) -> None:
        self.client = None
        self.connected = False
        self._discovered = False

    def close(self) -> None:
        """Publish offline and tear down (clean shutdown, not a crash)."""
        if self.client is None:
            return
        try:
            self.client.publish(availability_topic(), "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # pragma: no cover - best-effort teardown
            pass
        self._drop()


# Module-level singleton; the scheduler and the evaluator both publish through it.
publisher = MqttPublisher()
