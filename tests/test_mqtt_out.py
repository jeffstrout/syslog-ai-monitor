"""MQTT publishing + Home Assistant discovery (#15).

Nothing here needs a broker: the message builders are pure, and the publisher
takes an injected client. That is the same structure ac-monitor uses, and it is
what makes the discovery payloads testable at all.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app import db, mqtt_out


class FakeClient:
    """Records what a paho client would have been asked to do."""

    def __init__(self, fail_on_connect: bool = False):
        self.will: tuple | None = None
        self.credentials: tuple | None = None
        self.connected_to: tuple | None = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.published: list[tuple[str, str, bool]] = []
        self._fail_on_connect = fail_on_connect

    def will_set(self, topic, payload, retain=False):
        self.will = (topic, payload, retain)

    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def connect(self, host, port):
        if self._fail_on_connect:
            raise OSError("broker unreachable")
        self.connected_to = (host, port)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    # convenience for assertions
    def payload_for(self, topic):
        for t, p, _ in self.published:
            if t == topic:
                return p
        return None


def _settings(enabled=True, **over):
    base = dict(
        mqtt_enabled=enabled,
        mqtt_base_topic="syslog_monitor",
        mqtt_discovery_prefix="homeassistant",
        mqtt_host="broker.local",
        mqtt_port=1883,
        mqtt_user="",
        mqtt_pass="",
        eval_interval_minutes=60,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def mqtt_settings(monkeypatch):
    """settings is a frozen dataclass, so swap the module reference."""
    def _apply(**over):
        s = _settings(**over)
        monkeypatch.setattr(mqtt_out, "settings", s)
        return s
    return _apply


# --- discovery --------------------------------------------------------------

def test_discovery_covers_every_entity(mqtt_settings):
    mqtt_settings()
    msgs = discovery = mqtt_out.discovery_messages()
    keys = {t.split("/")[-2] for t, _, _ in discovery}

    assert keys == {
        "overall_status", "buffered_logs", "findings_stored",
        "last_evaluation", "evaluation_stale", "circuit_breaker",
    }
    assert all(retain for _, _, retain in msgs), "discovery must be retained"


def test_discovery_topics_follow_the_contract(mqtt_settings):
    mqtt_settings()
    topics = [t for t, _, _ in mqtt_out.discovery_messages()]

    assert any(t.startswith("homeassistant/sensor/syslog_monitor/") for t in topics)
    assert any(t.startswith("homeassistant/binary_sensor/syslog_monitor/") for t in topics)
    assert all(t.endswith("/config") for t in topics)


def test_every_entity_carries_availability_and_device(mqtt_settings):
    """Without availability_topic, HA cannot mark the device offline."""
    mqtt_settings()
    for _, payload, _ in mqtt_out.discovery_messages():
        p = json.loads(payload)
        assert p["availability_topic"] == "syslog_monitor/status"
        assert p["device"]["identifiers"] == ["syslog_monitor_pi"]
        assert p["unique_id"].startswith("syslog_monitor_")


def test_problem_entities_use_the_problem_device_class(mqtt_settings):
    """ON must mean 'there is a problem' for these two."""
    mqtt_settings()
    by_key = {t.split("/")[-2]: json.loads(p)
              for t, p, _ in mqtt_out.discovery_messages()}

    assert by_key["evaluation_stale"]["device_class"] == "problem"
    assert by_key["circuit_breaker"]["device_class"] == "problem"


def test_discovery_prefix_is_configurable(mqtt_settings):
    mqtt_settings(mqtt_discovery_prefix="ha", mqtt_base_topic="sysmon")
    topics = [t for t, _, _ in mqtt_out.discovery_messages()]
    assert all(t.startswith("ha/") for t in topics)
    assert json.loads(mqtt_out.discovery_messages()[0][1])["state_topic"].startswith("sysmon/")


# --- snapshot + state -------------------------------------------------------

def test_snapshot_reports_stale_when_no_evaluation_has_run(mqtt_settings):
    mqtt_settings()
    snap = mqtt_out.build_snapshot({"open": False})

    assert snap["overall_status"] == "unknown"
    assert snap["evaluation_stale"] is True
    assert snap["breaker_open"] is False


def test_snapshot_is_fresh_after_a_recent_finding(mqtt_settings):
    mqtt_settings()
    db.insert_finding(overall_status="warning", summary="s", log_count=3, payload={})
    snap = mqtt_out.build_snapshot({"open": True})

    assert snap["overall_status"] == "warning"
    assert snap["evaluation_stale"] is False
    assert snap["breaker_open"] is True
    assert snap["findings_stored"] == 1


def test_state_messages_render_booleans_as_on_off(mqtt_settings):
    mqtt_settings()
    msgs = dict((t, p) for t, p, _ in mqtt_out.state_messages({
        "overall_status": "error", "buffered_logs": 42, "findings_stored": 7,
        "last_evaluation_ts": None, "evaluation_stale": True, "breaker_open": False,
    }))

    assert msgs["syslog_monitor/status/overall"] == "error"
    assert msgs["syslog_monitor/counts/buffered_logs"] == "42"
    assert msgs["syslog_monitor/evaluation/stale"] == "ON"
    assert msgs["syslog_monitor/evaluation/breaker_open"] == "OFF"
    assert "syslog_monitor/evaluation/last" not in msgs   # omitted when never run


def test_last_evaluation_is_iso8601_for_the_timestamp_device_class(mqtt_settings):
    mqtt_settings()
    msgs = dict((t, p) for t, p, _ in mqtt_out.state_messages({
        "overall_status": "ok", "buffered_logs": 0, "findings_stored": 1,
        "last_evaluation_ts": 1750896000.0, "evaluation_stale": False,
        "breaker_open": False,
    }))
    value = msgs["syslog_monitor/evaluation/last"]

    assert value.endswith("+00:00")
    time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")   # parses, or raises


# --- publisher --------------------------------------------------------------

def test_lwt_is_registered_before_connecting(mqtt_settings):
    """The broker only honours a will supplied at connection time."""
    mqtt_settings()
    fake = FakeClient()
    mqtt_out.MqttPublisher(lambda: fake).publish({"open": False})

    assert fake.will == ("syslog_monitor/status", "offline", True)
    assert fake.connected_to == ("broker.local", 1883)
    assert fake.loop_started, "must use paho's own thread, not the event loop"


def test_online_is_published_retained_on_connect(mqtt_settings):
    mqtt_settings()
    fake = FakeClient()
    mqtt_out.MqttPublisher(lambda: fake).publish({"open": False})

    assert ("syslog_monitor/status", "online", True) in fake.published


def test_discovery_is_published_once_per_connection(mqtt_settings):
    mqtt_settings()
    fake = FakeClient()
    pub = mqtt_out.MqttPublisher(lambda: fake)

    pub.publish({"open": False})
    first = sum(1 for t, _, _ in fake.published if t.endswith("/config"))
    pub.publish({"open": False})
    second = sum(1 for t, _, _ in fake.published if t.endswith("/config"))

    assert first > 0 and second == first, "discovery re-sent on every tick"


def test_disabled_mqtt_never_creates_a_client(mqtt_settings):
    """No MQTT_HOST means no client and no error — same shape as SMTP."""
    mqtt_settings(enabled=False)
    created: list[int] = []

    def _factory():
        created.append(1)
        return FakeClient()

    mqtt_out.MqttPublisher(_factory).publish({"open": False})
    assert created == []


def test_a_broker_failure_does_not_propagate(mqtt_settings):
    """MQTT must never break evaluation."""
    mqtt_settings()
    pub = mqtt_out.MqttPublisher(lambda: FakeClient(fail_on_connect=True))

    pub.publish({"open": False})          # must not raise

    assert pub.client is None, "client dropped so the next tick retries"
    assert pub.connected is False


def test_credentials_are_set_only_when_configured(mqtt_settings):
    mqtt_settings()
    anon = FakeClient()
    mqtt_out.MqttPublisher(lambda: anon).publish({"open": False})
    assert anon.credentials is None

    mqtt_settings(mqtt_user="pi", mqtt_pass="secret")
    authed = FakeClient()
    mqtt_out.MqttPublisher(lambda: authed).publish({"open": False})
    assert authed.credentials == ("pi", "secret")


def test_close_publishes_offline_and_tears_down(mqtt_settings):
    """A clean stop must not leave HA showing the device as available."""
    mqtt_settings()
    fake = FakeClient()
    pub = mqtt_out.MqttPublisher(lambda: fake)
    pub.publish({"open": False})

    pub.close()

    assert ("syslog_monitor/status", "offline", True) in fake.published
    assert fake.loop_stopped and fake.disconnected
    assert pub.client is None
