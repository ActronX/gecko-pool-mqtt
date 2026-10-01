"""Requirements R-MQ: MQTT bridge, topics, validation, and shutdown.

Test cases TC-MQ-01 through TC-MQ-17. The paho client is replaced with a fake
so tests can run without a broker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import paho.mqtt.client as mqtt
import pytest

from app.config import settings
from app.mqtt_bridge import MqttBridge

from .fakes import FakeMqttMessageInfo, FakePahoClient

BASE = "gecko_test"


@dataclass
class BridgeHarness:
    bridge: MqttBridge
    client: FakePahoClient
    ctor_kwargs: dict[str, Any]
    dispatched: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    heat: list[dict[str, Any]] = field(default_factory=list)
    auth_response: list[str] = field(default_factory=list)
    auth_login: list[int] = field(default_factory=list)

    def json_payload(self, topic: str) -> Any:
        entry = self.client.last(topic)
        return None if entry is None else json.loads(entry["payload"])


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> BridgeHarness:
    """Bridge with a fake client instead of paho."""
    built: list[FakePahoClient] = []
    kwargs: dict[str, Any] = {}

    def client_factory(**client_kwargs: Any) -> FakePahoClient:
        kwargs.update(client_kwargs)
        client = FakePahoClient()
        built.append(client)
        return client

    monkeypatch.setattr(mqtt, "Client", client_factory)

    holder = BridgeHarness(bridge=None, client=None, ctor_kwargs={})  # type: ignore[arg-type]
    holder.dispatched = []
    holder.heat = []
    holder.auth_response = []
    holder.auth_login = []

    bridge = MqttBridge(
        on_command=lambda command_type, zone_id, data: holder.dispatched.append(
            (command_type, zone_id, data)
        ),
        on_heat_pump=lambda data: holder.heat.append(data),
        on_auth_response=lambda payload: holder.auth_response.append(payload),
        on_auth_login=lambda: holder.auth_login.append(1),
    )

    assert built, "The bridge constructor did not create a client"
    holder.bridge = bridge
    holder.client = built[-1]
    holder.ctor_kwargs = dict(kwargs)
    return holder


# --------------------------------------------------------------------------
# R-MQ-01 Topic construction
# --------------------------------------------------------------------------


def test_tc_mq_01_topic_uses_base_prefix(harness: BridgeHarness) -> None:
    """R-MQ-01: The topic starts with MQTT_BASE_TOPIC."""
    assert harness.bridge.topic("status/availability") == f"{BASE}/status/availability"


def test_tc_mq_01_topic_strips_leading_slash(harness: BridgeHarness) -> None:
    """R-MQ-01: Leading slashes are normalized."""
    assert harness.bridge.topic("/status/zones") == f"{BASE}/status/zones"
    assert harness.bridge.topic("///status/zones") == f"{BASE}/status/zones"
    assert harness.bridge.topic("status/zones") == harness.bridge.topic("status/zones")


def test_tc_mq_01_base_topic_trailing_slash_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """R-MQ-01: A trailing slash in MQTT_BASE_TOPIC is removed exactly once."""
    monkeypatch.setattr(settings, "mqtt_base_topic", "pool/")

    bridge = MqttBridge(lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None)

    assert bridge.base == "pool"
    assert bridge.topic("status/zones") == "pool/status/zones"


# --------------------------------------------------------------------------
# R-MQ-02 Connection setup
# --------------------------------------------------------------------------


def test_tc_mq_02_client_id_is_passed(harness: BridgeHarness) -> None:
    """R-MQ-02: The configured client ID is used."""
    assert harness.ctor_kwargs["client_id"] == "gecko-pool-mqtt-test"


def test_tc_mq_02_last_will_marks_offline(harness: BridgeHarness) -> None:
    """R-MQ-02: The last will marks an unexpected loss as offline."""
    assert harness.client.will == {
        "topic": f"{BASE}/status/availability",
        "payload": "offline",
        "qos": 1,
        "retain": True,
    }


def test_tc_mq_02_reconnect_delay_is_bounded(harness: BridgeHarness) -> None:
    """R-MQ-02: Reconnect backoff is bounded from 1 to 60 seconds."""
    assert harness.client.reconnect_delay == (1, 60)


def test_tc_mq_02_credentials_are_only_set_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """R-MQ-02: No login is configured without credentials."""
    monkeypatch.setattr(settings, "mqtt_username", "")
    built: list[FakePahoClient] = []

    def client_factory(**client_kwargs: Any) -> FakePahoClient:
        client = FakePahoClient()
        built.append(client)
        return client

    monkeypatch.setattr(mqtt, "Client", client_factory)

    MqttBridge(lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None)

    assert built[0].username is None
    assert built[0].password is None


def test_tc_mq_02_credentials_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """R-MQ-02: Configured credentials are forwarded to paho."""
    monkeypatch.setattr(settings, "mqtt_username", "user")
    monkeypatch.setattr(settings, "mqtt_password", "secret")
    built: list[FakePahoClient] = []

    def client_factory(**client_kwargs: Any) -> FakePahoClient:
        client = FakePahoClient()
        built.append(client)
        return client

    monkeypatch.setattr(mqtt, "Client", client_factory)

    MqttBridge(lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None)

    assert built[0].username == "user"
    assert built[0].password == "secret"


# --------------------------------------------------------------------------
# R-MQ-03, R-MQ-04 Response to connection setup
# --------------------------------------------------------------------------


def test_tc_mq_03_connect_subscribes_all_command_topics(harness: BridgeHarness) -> None:
    """R-MQ-03: After connect, all four command topics are subscribed."""
    harness.client.fire_connect()

    assert harness.client.subscribed == [
        [
            (f"{BASE}/cmd/+/+/set", 1),
            (f"{BASE}/cmd/heatPump", 1),
            (f"{BASE}/auth/response", 1),
            (f"{BASE}/auth/login", 1),
        ]
    ]


def test_tc_mq_04_connect_publishes_retained_online(harness: BridgeHarness) -> None:
    """R-MQ-04: After connect, retained online is published."""
    harness.client.fire_connect()

    entry = harness.client.last(f"{BASE}/status/availability")
    assert entry is not None
    assert entry["payload"] == "online"
    assert entry["retain"] is True
    assert entry["qos"] == 1


def test_tc_mq_04_failed_connect_does_not_publish_online(harness: BridgeHarness) -> None:
    """R-MQ-04: A rejected connection does not publish online."""
    harness.client.fire_connect(SimpleNamespace(is_failure=True))

    assert harness.client.published == []


# --------------------------------------------------------------------------
# R-MQ-05 through R-MQ-11 Dispatch and validation
# --------------------------------------------------------------------------


def test_tc_mq_05_valid_topic_is_dispatched(harness: BridgeHarness) -> None:
    """R-MQ-05: A valid command topic is dispatched."""
    harness.client.fire_message(f"{BASE}/cmd/flow/1/set", b'{"action":"on"}')

    assert harness.dispatched == [("flow", "1", {"action": "on"})]


def test_tc_mq_05_empty_payload_fails_validation(harness: BridgeHarness) -> None:
    """R-MQ-05: An empty payload produces an empty object and fails validation."""
    harness.client.fire_message(f"{BASE}/cmd/flow/1/set", b"")

    assert harness.dispatched == []
    assert harness.json_payload(f"{BASE}/cmd/flow/1/result")["success"] is False


@pytest.mark.parametrize("suffix", ["cmd/flow/1", "cmd/flow/1/set/extra", "cmd/set", "cmd/a/b/c/d/set"])
def test_tc_mq_05_wrong_topic_shape_is_ignored(harness: BridgeHarness, suffix: str) -> None:
    """R-MQ-05: Exactly three segments after cmd/ are dispatched."""
    harness.client.fire_message(f"{BASE}/{suffix}", b'{"action":"on"}')

    assert harness.dispatched == []


def test_tc_mq_06_temperature_requires_numeric_target(harness: BridgeHarness) -> None:
    """R-MQ-06: target_temperature must be numeric."""
    harness.client.fire_message(f"{BASE}/cmd/temperature/zone-1/set", b'{"target_temperature":"warm"}')

    assert harness.dispatched == []
    result = harness.json_payload(f"{BASE}/cmd/temperature/zone-1/result")
    assert result["success"] is False
    assert "target_temperature must be numeric" in result["message"]


def test_tc_mq_07_lighting_rejects_unknown_action(harness: BridgeHarness) -> None:
    """R-MQ-07: An unknown lighting action is rejected."""
    harness.client.fire_message(f"{BASE}/cmd/lighting/l1/set", b'{"action":"dim"}')

    assert harness.dispatched == []
    assert harness.json_payload(f"{BASE}/cmd/lighting/l1/result")["success"] is False


def test_tc_mq_07_lighting_rejects_effect_with_off(harness: BridgeHarness) -> None:
    """R-MQ-07: effect cannot be combined with action off."""
    harness.client.fire_message(f"{BASE}/cmd/lighting/l1/set", b'{"action":"off","effect":"rainbow"}')

    assert harness.dispatched == []
    result = harness.json_payload(f"{BASE}/cmd/lighting/l1/result")
    assert "effect cannot be used with lighting action off" in result["message"]


@pytest.mark.parametrize("value", [-1, 256, 12.5, "200"])
def test_tc_mq_07_lighting_rejects_out_of_range_colour(harness: BridgeHarness, value: Any) -> None:
    """R-MQ-07: Color channels and intensity must be integers from 0 to 255."""
    harness.client.fire_message(
        f"{BASE}/cmd/lighting/l1/set", json.dumps({"action": "on", "r": value}).encode()
    )

    assert harness.dispatched == []


def test_tc_mq_07_lighting_currently_accepts_boolean_colour(harness: BridgeHarness) -> None:
    """R-MQ-07: `true` is accepted as 1, unlike `speed` for flow.

    The validation for r/g/b/intensity does not reject `bool`, while `flow` does.
    This inconsistency is recorded in REQUIREMENTS.md under *Known gaps*. The
    test pins down the actual behavior so a later change is deliberate.
    """
    harness.client.fire_message(
        f"{BASE}/cmd/lighting/l1/set", b'{"action":"on","r":true}'
    )

    assert harness.dispatched == [("lighting", "l1", {"action": "on", "r": True})]


def test_tc_mq_07_lighting_accepts_valid_colour(harness: BridgeHarness) -> None:
    """R-MQ-07: Valid color values are dispatched."""
    harness.client.fire_message(
        f"{BASE}/cmd/lighting/l1/set", b'{"action":"on","r":255,"g":0,"b":40,"intensity":200}'
    )

    assert harness.dispatched == [("lighting", "l1", {"action": "on", "r": 255, "g": 0, "b": 40, "intensity": 200})]


def test_tc_mq_08_flow_rejects_non_numeric_speed(harness: BridgeHarness) -> None:
    """R-MQ-08: speed must be numeric."""
    harness.client.fire_message(f"{BASE}/cmd/flow/1/set", b'{"action":"on","speed":"fast"}')

    assert harness.dispatched == []
    assert "speed must be numeric" in harness.json_payload(f"{BASE}/cmd/flow/1/result")["message"]


def test_tc_mq_08_flow_rejects_boolean_speed(harness: BridgeHarness) -> None:
    """R-MQ-08: Python treats bool as int, so it is explicitly rejected."""
    harness.client.fire_message(f"{BASE}/cmd/flow/1/set", b'{"action":"on","speed":true}')

    assert harness.dispatched == []


def test_tc_mq_09_unknown_command_type_is_rejected(harness: BridgeHarness) -> None:
    """R-MQ-09: An unknown command type produces an error result."""
    harness.client.fire_message(f"{BASE}/cmd/steam/1/set", b'{"action":"on"}')

    assert harness.dispatched == []
    result = harness.json_payload(f"{BASE}/cmd/steam/1/result")
    assert "unsupported command type: steam" in result["message"]


def test_tc_mq_10_heat_pump_accepts_valid_command(harness: BridgeHarness) -> None:
    """R-MQ-10: A valid heat-pump command is dispatched."""
    harness.client.fire_message(f"{BASE}/cmd/heatPump", b'{"action":"on","duration":30}')

    assert harness.heat == [{"action": "on", "duration": 30}]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"action":"maybe"}',
        b'{"action":"on","duration":0}',
        b'{"action":"on","duration":-5}',
        b'{"action":"on","duration":1.5}',
        b'{"action":"on","duration":"30"}',
        b"not json",
    ],
)
def test_tc_mq_10_invalid_heat_pump_payload_is_rejected(harness: BridgeHarness, payload: bytes) -> None:
    """R-MQ-10: Invalid heat-pump payloads are rejected."""
    harness.client.fire_message(f"{BASE}/cmd/heatPump", payload)

    assert harness.heat == []
    assert harness.json_payload(f"{BASE}/cmd/heatPump/result")["success"] is False


def test_tc_mq_11_invalid_payload_does_not_stop_bridge(harness: BridgeHarness) -> None:
    """R-MQ-11: An error result does not stop the service."""
    harness.client.fire_message(f"{BASE}/cmd/flow/1/set", b'{"action":"nope"}')
    harness.client.fire_message(f"{BASE}/cmd/flow/2/set", b'{"action":"on"}')

    assert harness.dispatched == [("flow", "2", {"action": "on"})]


def test_tc_mq_05_auth_topics_are_dispatched(harness: BridgeHarness) -> None:
    """R-MQ-05: Auth response and auth login reach their callbacks."""
    harness.client.fire_message(f"{BASE}/auth/response", b"https://example.test/redirect?code=abc")
    harness.client.fire_message(f"{BASE}/auth/login", b"")

    assert harness.auth_response == ["https://example.test/redirect?code=abc"]
    assert harness.auth_login == [1]


# --------------------------------------------------------------------------
# R-MQ-12 publish
# --------------------------------------------------------------------------


def test_tc_mq_12_publish_returns_message_info(harness: BridgeHarness) -> None:
    """R-MQ-12: A successful publish returns MessageInfo."""
    info = harness.bridge.publish("status/connectivity", {"a": 1}, qos=1, retain=True)

    assert info is not None
    assert info.rc == mqtt.MQTT_ERR_SUCCESS


def test_tc_mq_12_publish_serialises_json(harness: BridgeHarness) -> None:
    """R-MQ-12: Dictionaries are published as compact JSON."""
    harness.bridge.publish("status/connectivity", {"a": 1, "b": "x"})

    assert harness.client.last(f"{BASE}/status/connectivity")["payload"] == '{"a":1,"b":"x"}'


def test_tc_mq_12_publish_keeps_string_payload(harness: BridgeHarness) -> None:
    """R-MQ-12: Strings are sent unchanged."""
    harness.bridge.publish("status/availability", "offline", qos=1, retain=True)

    assert harness.client.last(f"{BASE}/status/availability")["payload"] == "offline"


def test_tc_mq_12_publish_returns_none_on_error(harness: BridgeHarness) -> None:
    """R-MQ-12: A failed publish returns None."""
    harness.client.publish_rc = 1

    assert harness.bridge.publish("status/zones", {}) is None


# --------------------------------------------------------------------------
# R-MQ-13, R-MQ-14 Shutdown
# --------------------------------------------------------------------------


def test_tc_mq_13_stop_waits_for_acknowledgement_before_disconnect(harness: BridgeHarness) -> None:
    """R-MQ-13: Retained offline is confirmed before disconnect."""
    client = harness.client
    info = FakeMqttMessageInfo(on_wait=lambda: client.events.append("wait_for_publish"))
    client.set_next_message_info(info)

    harness.bridge.stop()

    assert client.events == ["publish", "wait_for_publish", "disconnect", "loop_stop"]
    assert info.wait_timeouts == [settings.mqtt_shutdown_publish_timeout]
    offline = client.last(f"{BASE}/status/availability")
    assert offline["payload"] == "offline"
    assert offline["retain"] is True
    assert offline["qos"] == 1


def test_tc_mq_14_stop_disconnects_after_unacknowledged_publish(harness: BridgeHarness) -> None:
    """R-MQ-14: Disconnect still completes cleanly without confirmation."""
    client = harness.client
    client.set_next_message_info(FakeMqttMessageInfo(acknowledged=False))

    harness.bridge.stop()

    assert client.events == ["publish", "disconnect", "loop_stop"]


def test_tc_mq_14_stop_survives_publish_exception(harness: BridgeHarness) -> None:
    """R-MQ-14: An exception while waiting does not stop shutdown."""
    client = harness.client
    client.set_next_message_info(FakeMqttMessageInfo(wait_raises=RuntimeError("boom")))

    harness.bridge.stop()

    assert client.events == ["publish", "disconnect", "loop_stop"]


def test_tc_mq_14_stop_survives_publish_failure(harness: BridgeHarness) -> None:
    """R-MQ-14: An impossible publish still calls disconnect and loop_stop."""
    client = harness.client
    client.publish_rc = 4

    harness.bridge.stop()

    assert client.events == ["publish", "disconnect", "loop_stop"]
    assert client.loop_stopped is True


# --------------------------------------------------------------------------
# R-MQ-other: Bridge helpers
# --------------------------------------------------------------------------


def test_tc_mq_16_heat_pump_events_are_not_retained(harness: BridgeHarness) -> None:
    """R-MQ-11: Reassert and error events are not retained."""
    harness.bridge.publish_heat_pump_reassert({"reason": "zone became inactive"})
    harness.bridge.publish_heat_pump_error({"error": "emergency_stop"})
    harness.bridge.publish_heat_pump_state({"state": "running"})

    assert harness.client.last(f"{BASE}/status/heatPump/reassert")["retain"] is False
    assert harness.client.last(f"{BASE}/status/heatPump/error")["retain"] is False
    assert harness.client.last(f"{BASE}/status/heatPump/state")["retain"] is True


def test_tc_mq_16_command_results_are_not_retained(harness: BridgeHarness) -> None:
    """R-MQ-11: Command acknowledgements are not retained."""
    harness.bridge.publish_result("flow", "4", True, "Flow command applied")

    entry = harness.client.last(f"{BASE}/cmd/flow/4/result")
    assert entry["retain"] is False
    assert json.loads(entry["payload"]) == {
        "success": True,
        "message": "Flow command applied",
        "zone_id": "4",
    }


def test_tc_mq_16_clear_challenge_sends_empty_retained_message(harness: BridgeHarness) -> None:
    """R-MQ-11: The challenge is deleted as an empty retained message."""
    harness.bridge.clear_challenge()

    entry = harness.client.last(f"{BASE}/auth/challenge")
    assert entry["payload"] == ""
    assert entry["retain"] is True


# --------------------------------------------------------------------------
# R-MQ-17 Trace
# --------------------------------------------------------------------------


def trace_entries() -> list[dict[str, Any]]:
    """Reads the daily files in the trace directory set by the fixture."""
    entries: list[dict[str, Any]] = []
    for day_file in sorted(Path(settings.mqtt_trace_dir).glob("mqtt-*.jsonl")):
        for line in day_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def test_tc_mq_17_outgoing_publish_is_recorded(harness: BridgeHarness) -> None:
    """R-MQ-17: Each publish enters the trace with topic, dir out, qos, and retain."""
    harness.bridge.publish_heat_pump_state({"state": "running", "zone_id": "4"})

    entry = trace_entries()[-1]
    assert entry["dir"] == "out"
    assert entry["topic"] == f"{BASE}/status/heatPump/state"
    assert json.loads(entry["payload"]) == {"state": "running", "zone_id": "4"}
    assert entry["qos"] == 1
    assert entry["retain"] is True
    assert entry["ts"].endswith("+00:00")


def test_tc_mq_17_incoming_message_is_recorded_before_dispatch(
    harness: BridgeHarness,
) -> None:
    """R-MQ-17: An incoming message is in the trace before it takes effect."""
    harness.client.fire_message(f"{BASE}/cmd/flow/4/set", b'{"action":"on"}')

    entry = trace_entries()[-1]
    assert entry["dir"] == "in"
    assert entry["topic"] == f"{BASE}/cmd/flow/4/set"
    assert entry["payload"] == '{"action":"on"}'
    assert harness.dispatched == [("flow", "4", {"action": "on"})]


def test_tc_mq_17_failed_publish_is_recorded(harness: BridgeHarness) -> None:
    """R-MQ-17: A failed send attempt is also recorded.

    Without it, the messages that indicate a broker error are missing.
    """
    harness.client.publish_rc = 4

    assert harness.bridge.publish("status/availability", "online", qos=1, retain=True) is None
    assert trace_entries()[-1]["topic"] == f"{BASE}/status/availability"


def test_tc_mq_17_auth_response_is_redacted(harness: BridgeHarness) -> None:
    """R-MQ-17: The OAuth code from auth/response is not stored in plain text."""
    harness.client.fire_message(f"{BASE}/auth/response", b"https://app.test/redirect?code=SECRET")

    entry = trace_entries()[-1]
    assert entry["topic"] == f"{BASE}/auth/response"
    assert entry["payload"] == "<redacted>"
    assert entry["bytes"] > 0
    raw = Path(settings.mqtt_trace_dir)
    for day_file in raw.glob("mqtt-*.jsonl"):
        assert "SECRET" not in day_file.read_text(encoding="utf-8")
    # The message still takes effect.
    assert harness.auth_response == ["https://app.test/redirect?code=SECRET"]


def test_tc_mq_17_disabled_trace_writes_nothing(
    harness: BridgeHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R-MQ-17: `off` disables the trace without disrupting operation."""
    monkeypatch.setattr(settings, "mqtt_trace_dir", "off")
    bridge = MqttBridge(lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None)

    bridge.publish("status/availability", "online", qos=1, retain=True)
    bridge.stop()

    assert list(tmp_path.rglob("*.jsonl")) == []
