"""Integration tests against a real MQTT broker (TC-INT-01 through TC-INT-06).

Voraussetzung::

    docker compose -f docker-compose.test.yml up -d
    pytest -m integration

Only the bridge's MQTT interface is tested. The Gecko client is a fake, and no
access to the Gecko cloud occurs.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable

import paho.mqtt.client as mqtt
import pytest
from gecko_iot_client import ZoneType

from app.config import settings
from app.mqtt_bridge import MqttBridge
from app.pool_controller import PoolController

from ..fakes import FakeGeckoClient, FakeZone

pytestmark = pytest.mark.integration


def availability() -> str:
    """Availability-Topic.

    Read from the settings on every call on purpose: ``isolated_settings``
    only sets ``MQTT_BASE_TOPIC`` at test time, a module constant would use
    the value from the real ``.env``.
    """
    return f"{settings.mqtt_base_topic}/status/availability"


class BrokerSubscriber:
    """Real paho client that reads the messages of a broker."""

    def __init__(self, host: str, port: int) -> None:
        self.messages: list[mqtt.MQTTMessage] = []
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"gecko-test-sub-{uuid.uuid4().hex[:8]}",
        )
        self.client.on_message = self._on_message
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        self.messages.append(message)

    def subscribe(self, topic: str) -> None:
        self.client.subscribe(topic, qos=1)

    async def wait_for(
        self,
        predicate: Callable[[mqtt.MQTTMessage], bool],
        description: str,
        timeout: float = 20.0,
    ) -> mqtt.MQTTMessage:
        """Waits asynchronously.

        Deliberately no ``time.sleep``: a blocking one would hold up the
        event loop, and the controller under test would never get anywhere.
        """
        deadline = time.monotonic() + timeout
        while True:
            for message in self.messages:
                if predicate(message):
                    return message
            if time.monotonic() > deadline:
                seen = [(m.topic, m.payload[:80]) for m in self.messages]
                raise AssertionError(f"Timeout while waiting for {description}. Seen: {seen}")
            await asyncio.sleep(0.05)

    async def wait_for_payload(self, topic: str, payload: str) -> mqtt.MQTTMessage:
        return await self.wait_for(
            lambda m: m.topic == topic and m.payload.decode() == payload,
            f"{topic} = {payload!r}",
        )

    async def wait_for_json(
        self, topic: str, check: Callable[[dict[str, Any]], bool], description: str
    ) -> dict[str, Any]:
        message = await self.wait_for(
            lambda m: m.topic == topic and _matches(m.payload, check), description
        )
        return json.loads(message.payload)

    def payloads(self, topic: str) -> list[str]:
        return [m.payload.decode() for m in self.messages if m.topic == topic]

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # pragma: no cover - cleanup only
            pass


def _matches(raw: bytes, check: Callable[[dict[str, Any]], bool]) -> bool:
    try:
        return check(json.loads(raw))
    except (ValueError, TypeError):
        return False


async def publish(host: str, port: int, topic: str, payload: Any) -> None:
    """Sends a message and waits for its confirmation."""

    def send() -> None:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"gecko-test-pub-{uuid.uuid4().hex[:8]}",
        )
        client.connect(host, port, keepalive=30)
        client.loop_start()
        try:
            info = client.publish(topic, json.dumps(payload), qos=1)
            info.wait_for_publish(timeout=15)
        finally:
            client.disconnect()
            client.loop_stop()

    await asyncio.to_thread(send)


@pytest.fixture
async def stack(monkeypatch: pytest.MonkeyPatch, mqtt_broker: tuple[str, int]):
    """Bridge plus Controller with a faked Gecko client."""
    host, port = mqtt_broker
    # The bridge must reach the broker inside the container, not 127.0.0.1.
    monkeypatch.setattr(settings, "mqtt_host", host)
    monkeypatch.setattr(settings, "mqtt_port", port)
    holder: dict[str, Any] = {}

    bridge = MqttBridge(
        on_command=lambda command_type, zone_id, data: holder["controller"].handle_command(
            command_type, zone_id, data
        ),
        on_heat_pump=lambda data: holder["controller"].handle_heat_pump_command(data),
        on_auth_response=lambda payload: holder["controller"].handle_auth_response(payload),
        on_auth_login=lambda: holder["controller"].handle_auth_login(),
    )
    controller = PoolController(asyncio.get_running_loop(), bridge)
    holder["controller"] = controller

    client = FakeGeckoClient(connected=True)
    controller.client = client
    controller.authenticated = True

    yield bridge, controller, client, (host, port)

    await controller.heat_pump.stop()
    bridge.stop()


def make_zone(client: FakeGeckoClient, zone_type: Any, zone_id: str, **kwargs: Any) -> FakeZone:
    return client.add_zone(FakeZone(zone_id, **kwargs), zone_type)


# --------------------------------------------------------------------------
# TC-INT-01 Verbindungsaufbau
# --------------------------------------------------------------------------


async def test_tc_int_01_bridge_publishes_retained_online(stack) -> None:
    """R-MQ-03, R-MQ-04: The bridge reports retained online."""
    bridge, _controller, _client, (host, port) = stack
    subscriber = BrokerSubscriber(host, port)
    try:
        subscriber.subscribe(availability())
        bridge.start()
        await subscriber.wait_for_payload(availability(), "online")
    finally:
        subscriber.close()


async def test_tc_int_01_online_is_retained_for_late_subscribers(stack) -> None:
    """R-MQ-04: online is retained and delivered to later subscribers."""
    bridge, _controller, _client, (host, port) = stack
    bridge.start()

    first = BrokerSubscriber(host, port)
    try:
        first.subscribe(availability())
        await first.wait_for_payload(availability(), "online")
    finally:
        first.close()

    second = BrokerSubscriber(host, port)
    try:
        second.subscribe(availability())
        message = await second.wait_for_payload(availability(), "online")
        assert message.retain is True
    finally:
        second.close()


# --------------------------------------------------------------------------
# TC-INT-02, TC-INT-03 Kommando-Roundtrip
# --------------------------------------------------------------------------


async def test_tc_int_02_flow_command_round_trip(stack) -> None:
    """R-CM-05, R-CM-09: A flow command reaches the zone and is acknowledged."""
    bridge, _controller, client, (host, port) = stack
    zone = make_zone(client, ZoneType.FLOW_ZONE, "1")
    topic = f"{settings.mqtt_base_topic}/cmd/flow/1/result"
    subscriber = BrokerSubscriber(host, port)
    try:
        subscriber.subscribe(topic)
        subscriber.subscribe(availability())
        bridge.start()
        await subscriber.wait_for_payload(availability(), "online")

        await publish(host, port, f"{settings.mqtt_base_topic}/cmd/flow/1/set", {"action": "on"})

        payload = await subscriber.wait_for_json(
            topic,
            lambda data: data.get("success") is True,
            "erfolgreiches flow-result",
        )
        assert payload == {
            "success": True,
            "message": "Flow command applied",
            "zone_id": "1",
        }
        assert zone.count("activate") == 1
    finally:
        subscriber.close()


async def test_tc_int_02_temperature_command_round_trip(stack) -> None:
    """R-CM-02: A temperature command is acknowledged and applied."""
    bridge, _controller, client, (host, port) = stack
    zone = make_zone(client, ZoneType.TEMPERATURE_CONTROL_ZONE, "zone-1")
    topic = f"{settings.mqtt_base_topic}/cmd/temperature/zone-1/result"
    subscriber = BrokerSubscriber(host, port)
    try:
        subscriber.subscribe(topic)
        subscriber.subscribe(availability())
        bridge.start()
        await subscriber.wait_for_payload(availability(), "online")

        await publish(
            host,
            port,
            f"{settings.mqtt_base_topic}/cmd/temperature/zone-1/set",
            {"target_temperature": 28.0},
        )

        payload = await subscriber.wait_for_json(
            topic, lambda data: data.get("success") is True, "erfolgreiches temperature-result"
        )
        assert payload["message"] == "Target temperature set to 28.0"
        assert zone.last_args("set_target_temperature") == ((28.0,), {})
    finally:
        subscriber.close()


async def test_tc_int_03_invalid_payload_is_answered_and_bridge_survives(stack) -> None:
    """R-MQ-11: An invalid payload yields an error result; the bridge keeps running."""
    bridge, _controller, _client, (host, port) = stack
    topic = f"{settings.mqtt_base_topic}/cmd/flow/1/result"
    subscriber = BrokerSubscriber(host, port)
    try:
        subscriber.subscribe(topic)
        subscriber.subscribe(availability())
        bridge.start()
        await subscriber.wait_for_payload(availability(), "online")

        await publish(host, port, f"{settings.mqtt_base_topic}/cmd/flow/1/set", {"action": "sideways"})

        payload = await subscriber.wait_for_json(
            topic,
            lambda data: data.get("success") is False,
            "Error result for flow",
        )
        assert "action must be on or off" in payload["message"]

        # The bridge is still reachable afterwards.
        await publish(host, port, f"{settings.mqtt_base_topic}/cmd/flow/1/set", {"action": "off"})
    finally:
        subscriber.close()


# --------------------------------------------------------------------------
# TC-INT-04 Waermepumpe
# --------------------------------------------------------------------------


async def test_tc_int_04_heat_pump_command_round_trip(stack) -> None:
    """R-HP-01: A heat-pump command reaches waiting_confirmation and running."""
    bridge, controller, client, (host, port) = stack
    zone = make_zone(client, ZoneType.FLOW_ZONE, settings.heat_pump_flow_zone_id)
    result_topic = f"{settings.mqtt_base_topic}/cmd/heatPump/result"
    state_topic = f"{settings.mqtt_base_topic}/status/heatPump/state"
    subscriber = BrokerSubscriber(host, port)
    try:
        subscriber.subscribe(result_topic)
        subscriber.subscribe(state_topic)
        subscriber.subscribe(availability())
        bridge.start()
        await subscriber.wait_for_payload(availability(), "online")

        await publish(host, port, f"{settings.mqtt_base_topic}/cmd/heatPump", {"action": "on", "duration": 30})

        result = await subscriber.wait_for_json(
            result_topic, lambda data: data.get("success") is True, "erfolgreiches heatPump-result"
        )
        assert result["action"] == "on"
        assert result["duration"] == 30
        assert result["zone_id"] == settings.heat_pump_flow_zone_id
        assert 1790 <= result["remaining_seconds"] <= 1800
        assert zone.count("activate") == 1

        await subscriber.wait_for_json(
            state_topic,
            lambda data: data.get("state") == "waiting_confirmation",
            "waiting_confirmation",
        )

        # Simulate the confirmation: the zone reports active true.
        zone.active = True
        controller.heat_pump.observe_zone_update({ZoneType.FLOW_ZONE: [zone]})

        running = await subscriber.wait_for_json(
            state_topic, lambda data: data.get("state") == "running", "running"
        )
        assert running["armed"] is True
    finally:
        subscriber.close()


# --------------------------------------------------------------------------
# TC-INT-05, TC-INT-06 Graceful Shutdown
# --------------------------------------------------------------------------


async def test_tc_int_05_retained_availability_becomes_offline(stack) -> None:
    """R-MQ-13, R-OPS-03: After the shutdown the retained topic is offline.

    A fresh subscriber reads the retained value. Precisely this case used to
    get lost when the last message before the disconnect was not confirmed.
    """
    bridge, _controller, _client, (host, port) = stack
    bridge.start()

    warmup = BrokerSubscriber(host, port)
    try:
        warmup.subscribe(availability())
        await warmup.wait_for_payload(availability(), "online")
    finally:
        warmup.close()

    bridge.stop()

    after = BrokerSubscriber(host, port)
    try:
        after.subscribe(availability())
        message = await after.wait_for_payload(availability(), "offline")
        assert message.retain is True
    finally:
        after.close()


async def test_tc_int_06_shutdown_publishes_offline_exactly_once(stack) -> None:
    """R-OPS-03: After the shutdown no further online is published."""
    bridge, _controller, _client, (host, port) = stack
    bridge.start()

    warmup = BrokerSubscriber(host, port)
    try:
        warmup.subscribe(availability())
        await warmup.wait_for_payload(availability(), "online")
    finally:
        warmup.close()

    bridge.stop()

    after = BrokerSubscriber(host, port)
    try:
        after.subscribe(availability())
        await after.wait_for_payload(availability(), "offline")
        assert after.payloads(availability()) == ["offline"]
    finally:
        after.close()


async def test_tc_int_06_bridge_can_be_started_after_shutdown(stack) -> None:
    """R-MQ-02: The bridge can be started again after a shutdown."""
    bridge, _controller, _client, (host, port) = stack
    bridge.start()

    warmup = BrokerSubscriber(host, port)
    try:
        warmup.subscribe(availability())
        await warmup.wait_for_payload(availability(), "online")
    finally:
        warmup.close()

    bridge.stop()
    bridge.start()

    after = BrokerSubscriber(host, port)
    try:
        after.subscribe(availability())
        await after.wait_for_payload(availability(), "online")
    finally:
        after.close()
