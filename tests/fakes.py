"""Test fakes for the bridge.

The ``gecko-iot-client`` library is **not** tested by these fakes. They only
mirror the signatures that ``app/`` actually uses:

* ``GeckoIotClient`` with ``connect`` / ``disconnect`` / ``is_connected`` /
  ``get_zone_by_id_and_type`` / ``get_zones`` / ``on`` / ``emit``
* zone objects with ``activate``, ``deactivate``, ``set_speed``,
  ``set_color``, ``set_effect`` and ``set_target_temperature``
* ``MQTTMessageInfo`` with ``wait_for_publish`` and ``is_published``

In the real library, zone methods are synchronous and block for up to five
seconds waiting for PUBACK confirmation. The fakes reproduce this behavior
through ``block``.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

# --------------------------------------------------------------------------
# Zones
# --------------------------------------------------------------------------


class FakeZone:
    """Fake Gecko zone object.

    Each mutation method is synchronous, records the call in ``calls``, and
    can block or raise an exception to reproduce library behavior.
    """

    def __init__(
        self,
        zone_id: str = "1",
        *,
        name: str = "Fake Zone",
        active: bool = False,
        initiators: list[Any] | None = None,
        speed_config: dict[str, Any] | None = None,
        capabilities: list[Any] | None = None,
        presets: list[Any] | None = None,
        speed: int | None = None,
        zone_type: Any = None,
    ) -> None:
        self.id = zone_id
        self.name = name
        self.active = active
        self.initiators = list(initiators or [])
        self.speed_config = speed_config
        self.capabilities = list(capabilities or [])
        self.presets = list(presets or [])
        self.speed = speed
        self.zone_type = zone_type

        # Recording
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        # Blocking / error per method
        self._gates: dict[str, threading.Event] = {}
        self._entered: dict[str, threading.Event] = {}
        self._delays: dict[str, float] = {}
        self._errors: dict[str, Exception] = {}
        self._gate_timeout = 10.0

    # -- Teststeuerung ---------------------------------------------------

    def block(self, method: str, timeout: float = 10.0) -> threading.Event:
        """Blocks ``method`` until the returned event is set."""
        gate = self._gates.get(method)
        if gate is None:
            gate = threading.Event()
            self._gates[method] = gate
        self._gate_timeout = timeout
        return gate

    def entered(self, method: str) -> threading.Event:
        """Event set as soon as ``method`` is entered."""
        event = self._entered.get(method)
        if event is None:
            event = threading.Event()
            self._entered[method] = event
        return event

    def delay(self, method: str, seconds: float) -> None:
        """Makes ``method`` sleep for ``seconds`` without being released."""
        self._delays[method] = seconds

    def fail(self, method: str, error: Exception) -> None:
        """Makes ``method`` raise the given exception."""
        self._errors[method] = error

    def release(self, method: str) -> None:
        """Releases a blocked method."""
        gate = self._gates.get(method)
        if gate is not None:
            gate.set()

    def call_names(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls]

    def count(self, method: str) -> int:
        return self.call_names().count(method)

    def last_args(self, method: str) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
        for name, args, kwargs in reversed(self.calls):
            if name == method:
                return args, kwargs
        return None

    # -- Gecko-API -------------------------------------------------------

    def _invoke(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((method, args, kwargs))
        entered = self._entered.get(method)
        if entered is not None:
            entered.set()
        delay = self._delays.get(method)
        if delay:
            time.sleep(delay)
        gate = self._gates.get(method)
        if gate is not None:
            gate.wait(timeout=self._gate_timeout)
        error = self._errors.get(method)
        if error is not None:
            raise error

    def activate(self) -> None:
        self._invoke("activate")

    def deactivate(self) -> None:
        self._invoke("deactivate")

    def set_speed(self, speed: float, active: bool = False) -> None:
        self._invoke("set_speed", speed, active=active)

    def set_color(self, r: int, g: int, b: int, intensity: int | None = None) -> None:
        self._invoke("set_color", r, g, b, intensity)

    def set_effect(self, effect: str) -> None:
        self._invoke("set_effect", effect)

    def set_target_temperature(self, temperature: float) -> None:
        self._invoke("set_target_temperature", temperature)


# Zone classes for the serializer. `app/zone_serializer.py` checks with
# `isinstance`; tests patch these names into the module.


class FakeTemperatureControlZone(FakeZone):
    def __init__(self, zone_id: str = "1", **kwargs: Any) -> None:
        kwargs.setdefault("name", "Temperature")
        super().__init__(zone_id, **kwargs)
        self.temperature = 27.5
        self.target_temperature = 28.0
        self.status = SimpleNamespace(name="READY")
        self.mode = SimpleNamespace(eco=False)
        self.min_temperature_set_point_c_value = 15.0
        self.max_temperature_set_point_c_value = 40.0


class FakeLightingZone(FakeZone):
    def __init__(self, zone_id: str = "1", **kwargs: Any) -> None:
        kwargs.setdefault("name", "Lighting")
        super().__init__(zone_id, **kwargs)
        self.rgbi = None
        self.effect = None


class FakeFlowZone(FakeZone):
    def __init__(self, zone_id: str = "1", **kwargs: Any) -> None:
        kwargs.setdefault("name", "Flow")
        kwargs.setdefault("zone_type", None)
        super().__init__(zone_id, **kwargs)
        self.speed = 50


class FakeUnknownZone(FakeZone):
    """Zone type unknown to the serializer."""


class FakeZoneType:
    """Hashable fake for an unknown ``ZoneType``.

    ``SimpleNamespace`` is not hashable and therefore cannot serve as a
    dictionary key or a key in ``ZONE_TYPE_LABELS``.
    """

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeZoneType) and other.value == self.value

    def __hash__(self) -> int:
        return hash(("FakeZoneType", self.value))

    def __repr__(self) -> str:
        return f"FakeZoneType({self.value!r})"


# --------------------------------------------------------------------------
# Gecko-Client
# --------------------------------------------------------------------------


class FakeGeckoClient:
    """Fake ``GeckoIotClient``."""

    def __init__(self, *, connected: bool = True) -> None:
        self.is_connected = connected
        self.zones: dict[Any, list[FakeZone]] = {}
        self._by_key: dict[tuple[Any, str], FakeZone] = {}
        self._handlers: dict[Any, list[Callable[..., Any]]] = {}
        self.calls: list[str] = []
        self.connectivity_status = SimpleNamespace(to_dict=lambda: {"is_fully_connected": connected})
        self.operation_mode_controller = SimpleNamespace(to_dict=lambda: {"mode": "STANDARD"})

    # -- Teststeuerung ---------------------------------------------------

    def add_zone(self, zone: FakeZone, zone_type: Any) -> FakeZone:
        self.zones.setdefault(zone_type, []).append(zone)
        self._by_key[(zone_type, str(zone.id))] = zone
        return zone

    def emit(self, event: Any, payload: Any) -> None:
        """Calls registered callbacks as the library does for updates."""
        for callback in list(self._handlers.get(event, [])):
            callback(payload)

    def connected_count(self) -> int:
        return self.calls.count("connect")

    def disconnected_count(self) -> int:
        return self.calls.count("disconnect")

    # -- Gecko-API -------------------------------------------------------

    def connect(self) -> None:
        self.calls.append("connect")
        self.is_connected = True

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.is_connected = False

    def get_zone_by_id_and_type(self, zone_type: Any, zone_id: str) -> FakeZone:
        self.calls.append("get_zone_by_id_and_type")
        try:
            return self._by_key[(zone_type, str(zone_id))]
        except KeyError:
            raise KeyError(f"No zone {zone_id} of type {zone_type}") from None

    def get_zones(self) -> dict[Any, list[FakeZone]]:
        self.calls.append("get_zones")
        return dict(self.zones)

    def on(self, event: Any, callback: Callable[..., Any]) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def get_all_zones(self) -> list[FakeZone]:
        return [zone for zones in self.zones.values() for zone in zones]


# --------------------------------------------------------------------------
# paho
# --------------------------------------------------------------------------


class FakeMqttMessageInfo:
    """Fake ``paho.mqtt.client.MQTTMessageInfo``."""

    def __init__(
        self,
        rc: int = 0,
        *,
        acknowledged: bool = True,
        wait_block_seconds: float = 0.0,
        wait_raises: Exception | None = None,
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        self.rc = rc
        self._acknowledged = acknowledged
        self._wait_block_seconds = wait_block_seconds
        self._wait_raises = wait_raises
        self._on_wait = on_wait
        self.wait_timeouts: list[float | None] = []

    def is_published(self) -> bool:
        return self._acknowledged

    def wait_for_publish(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self._on_wait is not None:
            self._on_wait()
        if self._wait_block_seconds:
            time.sleep(self._wait_block_seconds)
        if self._wait_raises is not None:
            raise self._wait_raises
        return self.rc


class FakePahoClient:
    """Fake local paho client used by ``MqttBridge``."""

    def __init__(self, *, publish_rc: int = 0) -> None:
        self.publish_rc = publish_rc
        self.published: list[dict[str, Any]] = []
        self.subscribed: list[Any] = []
        self.events: list[str] = []
        self.connected_to: tuple[str, int, int] | None = None
        self.username: str | None = None
        self.password: str | None = None
        self.will: dict[str, Any] | None = None
        self.reconnect_delay: tuple[int, int] | None = None
        self.disconnected = False
        self.loop_stopped = False
        self.next_message_info: FakeMqttMessageInfo | None = None
        # Callback attributes that MqttBridge assigns
        self.on_connect: Callable[..., Any] | None = None
        self.on_disconnect: Callable[..., Any] | None = None
        self.on_message: Callable[..., Any] | None = None

    # -- controlled by the test -------------------------------------------

    def set_next_message_info(self, info: FakeMqttMessageInfo) -> None:
        self.next_message_info = info

    def fire_connect(self, reason_code: Any = None) -> None:
        if self.on_connect is None:
            return
        self.on_connect(self, None, {"session present": 0}, reason_code or SimpleNamespace(is_failure=False))

    def fire_message(self, topic: str, payload: bytes) -> None:
        if self.on_message is None:
            return
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))

    # -- paho-API --------------------------------------------------------

    def username_pw_set(self, username: str | None, password: str | None = None) -> None:
        self.username = username
        self.password = password

    def will_set(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False) -> None:
        self.will = {"topic": topic, "payload": payload, "qos": qos, "retain": retain}

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.events.append("loop_start")

    def loop_stop(self) -> None:
        self.events.append("loop_stop")
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.events.append("disconnect")
        self.disconnected = True

    def subscribe(self, topics: Any) -> Any:
        self.subscribed.append(topics)
        return (0, 0)

    def publish(self, topic: str, payload: Any, qos: int = 0, retain: bool = False) -> FakeMqttMessageInfo:
        self.events.append("publish")
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        if self.next_message_info is not None:
            return self.next_message_info
        return FakeMqttMessageInfo(self.publish_rc)

    # -- Auswertungshilfen -----------------------------------------------

    def payloads(self, topic: str) -> list[Any]:
        return [entry["payload"] for entry in self.published if entry["topic"] == topic]

    def last(self, topic: str) -> dict[str, Any] | None:
        for entry in reversed(self.published):
            if entry["topic"] == topic:
                return entry
        return None


# --------------------------------------------------------------------------
# Bridge fake for the controllers
# --------------------------------------------------------------------------


class FakeMqttBridge:
    """Records all controller publications."""

    def __init__(self, base: str = "gecko") -> None:
        self.base = base
        self.heat_pump_results: list[dict[str, Any]] = []
        self.heat_pump_states: list[dict[str, Any]] = []
        self.heat_pump_reasserts: list[dict[str, Any]] = []
        self.heat_pump_errors: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.snapshots: list[Any] = []
        self.connectivity: list[dict[str, Any]] = []
        self.operation_modes: list[dict[str, Any]] = []
        self.auth_status: list[dict[str, Any]] = []
        self.challenges: list[dict[str, Any]] = []
        self.challenge_cleared = 0

    def topic(self, suffix: str) -> str:
        return f"{self.base}/{suffix.lstrip('/')}"

    # -- controller interface ---------------------------------------------

    def publish_heat_pump_result(self, success: bool, message: str, **details: Any) -> None:
        self.heat_pump_results.append({"success": success, "message": message, **details})

    def publish_heat_pump_state(self, payload: dict[str, Any]) -> None:
        self.heat_pump_states.append(dict(payload))

    def publish_heat_pump_reassert(self, payload: dict[str, Any]) -> None:
        self.heat_pump_reasserts.append(dict(payload))

    def publish_heat_pump_error(self, payload: dict[str, Any]) -> None:
        self.heat_pump_errors.append(dict(payload))

    def publish_result(self, command_type: str, zone_id: str, success: bool, message: str) -> None:
        self.results.append(
            {
                "command_type": command_type,
                "zone_id": zone_id,
                "success": success,
                "message": message,
            }
        )

    def publish_snapshot(self, zones: dict[str, Any]) -> None:
        self.snapshots.append(zones)

    def publish_connectivity(self, payload: dict[str, Any]) -> None:
        self.connectivity.append(dict(payload))

    def publish_operation_mode(self, payload: dict[str, Any]) -> None:
        self.operation_modes.append(dict(payload))

    def publish_auth_status(self, payload: dict[str, Any]) -> None:
        self.auth_status.append(dict(payload))

    def publish_challenge(self, authorize_url: str, state: str) -> None:
        self.challenges.append({"authorize_url": authorize_url, "state": state})

    def clear_challenge(self) -> None:
        self.challenge_cleared += 1

    # -- Auswertungshilfen -----------------------------------------------

    def state_messages(self) -> list[str]:
        return [payload["state"] for payload in self.heat_pump_states]

    def reassert_reasons(self) -> list[str]:
        return [payload["reason"] for payload in self.heat_pump_reasserts]


# --------------------------------------------------------------------------
# aiohttp-Attrappen
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int = 200, json_data: Any = None, text: str = "") -> None:
        self.status = status
        self._json = json_data
        self._text = text

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    async def json(self) -> Any:
        return self._json

    async def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeWebSession:
    """Fake ``aiohttp.ClientSession`` for OAuth and API tests."""

    def __init__(self) -> None:
        self.post_queue: list[FakeResponse] = []
        self.get_queue: list[FakeResponse] = []
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def queue_post(self, response: FakeResponse) -> None:
        self.post_queue.append(response)

    def queue_get(self, response: FakeResponse) -> None:
        self.get_queue.append(response)

    def post(self, url: str, json: Any = None) -> Any:
        self.posts.append({"url": url, "json": json})
        return self.post_queue.pop(0) if self.post_queue else FakeResponse(200, {})

    def get(self, url: str, headers: Any = None) -> Any:
        self.gets.append({"url": url, "headers": headers})
        return self.get_queue.pop(0) if self.get_queue else FakeResponse(200, {})

    async def close(self) -> None:
        return None


class FakeOAuthFlow:
    """Fake ``OAuthFlow`` for controller tests without HTTP."""

    def __init__(self, *, has_tokens: bool = True) -> None:
        self._has_tokens = has_tokens
        self.exchanged: list[tuple[str, str | None]] = []
        self.cleared = False

    def has_tokens(self) -> bool:
        return self._has_tokens

    def build_authorize_url(self) -> tuple[str, str]:
        return ("https://auth.example/authorize?x=1", "state-1")

    async def exchange_code(self, code: str, state: str | None = None) -> dict[str, Any]:
        self.exchanged.append((code, state))
        return {"access_token": "token"}

    async def get_valid_access_token(self) -> str:
        return "token"

    def clear_tokens(self) -> None:
        self.cleared = True
