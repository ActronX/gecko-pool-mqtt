"""Shared test setup.

The tests run without network access to the Gecko cloud. The only integration
test that opens a real TCP connection uses the broker from
``docker-compose.test.yml`` and is skipped when it is not running.

The prerequisite is a Python 3.13 environment with the runtime dependencies
from ``requirements.txt`` installed.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import pytest
from gecko_iot_client import ZoneType

from app.config import settings
from app.heat_pump_controller import HeatPumpController
from app.pool_controller import PoolController

from .fakes import (
    FakeGeckoClient,
    FakeMqttBridge,
    FakeOAuthFlow,
    FakeWebSession,
    FakeZone,
)

TEST_BASE_TOPIC = "gecko_test"
TEST_ZONE_ID = "4"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Any:
    """Sets all settings influenced by tests to controlled values.

    The repository contains a real ``.env`` that is loaded into the settings
    singleton when ``app.config`` is imported. Without this fixture, tests
    would write to ``/data/tokens.json``, for example.
    """
    monkeypatch.setattr(settings, "mqtt_base_topic", TEST_BASE_TOPIC)
    monkeypatch.setattr(settings, "mqtt_client_id", "gecko-pool-mqtt-test")
    monkeypatch.setattr(settings, "mqtt_host", "127.0.0.1")
    monkeypatch.setattr(settings, "mqtt_port", 1883)
    monkeypatch.setattr(settings, "mqtt_username", "")
    monkeypatch.setattr(settings, "mqtt_password", "")
    monkeypatch.setattr(settings, "mqtt_shutdown_publish_timeout", 0.5)
    monkeypatch.setattr(settings, "oauth_token_file", str(tmp_path / "tokens.json"))
    monkeypatch.setattr(settings, "oauth_client_id", "test-client-id")
    monkeypatch.setattr(settings, "oauth_redirect_uri", "https://example.test/redirect")
    monkeypatch.setattr(settings, "auth0_url", "https://auth.test")
    monkeypatch.setattr(settings, "api_url", "https://api.test")
    monkeypatch.setattr(settings, "heat_pump_flow_zone_id", TEST_ZONE_ID)
    monkeypatch.setattr(settings, "heat_pump_default_duration", 30)
    monkeypatch.setattr(settings, "heat_pump_max_reassert_attempts", 2)
    monkeypatch.setattr(settings, "heat_pump_check_interval", 0.01)
    monkeypatch.setattr(settings, "heat_pump_confirm_timeout", 0.05)
    # Without this line, bridge tests would write to the container's real /tmp
    # instead of the test's temporary directory.
    monkeypatch.setattr(settings, "mqtt_trace_dir", str(tmp_path / "mqtt-trace"))
    monkeypatch.setattr(settings, "config_timeout", 0.05)
    monkeypatch.setattr(settings, "gecko_recovery_delay", 0.01)
    return settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str | None], None]:
    """Sets environment variables for settings-factory tests."""

    def setter(name: str, value: str | None) -> None:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    return setter


# --------------------------------------------------------------------------
# Broker
# --------------------------------------------------------------------------


def _broker_available(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def broker_address() -> tuple[str, int]:
    host = os.getenv("MQTT_TEST_HOST", "127.0.0.1")
    port = int(os.getenv("MQTT_TEST_PORT", "1883"))
    return host, port


@pytest.fixture
def mqtt_broker(broker_address: tuple[str, int]) -> tuple[str, int]:
    host, port = broker_address
    if not _broker_available(host, port):
        pytest.skip(
            f"No MQTT broker on {host}:{port}. "
            "Start with: docker compose -f docker-compose.test.yml up -d"
        )
    return host, port


# --------------------------------------------------------------------------
# Waermepumpe
# --------------------------------------------------------------------------


@dataclass
class HeatPumpHarness:
    """Controller, fakes, and observation points for a heat-pump test."""

    controller: HeatPumpController
    mqtt: FakeMqttBridge
    client: FakeGeckoClient
    zone: FakeZone
    wait_connected_calls: int = 0
    authenticated: bool = True
    loop: asyncio.AbstractEventLoop | None = None
    extras: list[Any] = field(default_factory=list)
    wait_until_connected: Callable[[], Awaitable[None]] | None = None

    async def on(self, **payload: Any) -> None:
        """Runs an ``action: on`` command."""
        await self.controller._handle_command({"action": "on", **payload})

    async def off(self) -> None:
        await self.controller._handle_command({"action": "off"})

    def zone_update(self, active: bool) -> None:
        """Simulates a Gecko zone update for the flow zone."""
        self.zone.active = active
        self.controller._observe_zone_update({ZoneType.FLOW_ZONE: [self.zone]})

    def quiet_watchdog(self, confirm_timeout: float = 5.0, max_attempts: int = 5) -> None:
        """Uses a long confirmation timeout so only zone updates reassert.

        Without this setting, the watchdog also triggers ``activation
        confirmation timeout`` during ``settle()`` and the activate count is
        indeterminate.
        """
        settings.heat_pump_confirm_timeout = confirm_timeout
        settings.heat_pump_max_reassert_attempts = max_attempts

    async def settle(self, cycles: int = 3) -> None:
        """Lets the watchdog run for several cycles."""
        for _ in range(cycles):
            await asyncio.sleep(settings.heat_pump_check_interval * 2)

    def restart(self) -> None:
        """Simulates a process restart on the same fakes.

        The previous controller is dropped the way a crash drops it: its
        watchdog task is cancelled, but ``stop()`` never runs, so no
        ``deactivate()`` reaches the fake zone. The zone therefore keeps
        reporting ``active: True`` with the initiator it had before the crash,
        exactly as it does in the cloud after an abrupt process death.
        """
        self.controller._cancel_watchdog()
        self.controller = HeatPumpController(
            self.loop,
            self.mqtt,
            lambda: self.client,
            lambda: self.authenticated,
            self.wait_until_connected,  # type: ignore[arg-type]
        )


@pytest.fixture
async def heat_pump(isolated_settings) -> HeatPumpHarness:
    """Heat-pump controller with fakes and fast timings."""
    loop = asyncio.get_running_loop()
    mqtt = FakeMqttBridge(base=TEST_BASE_TOPIC)
    client = FakeGeckoClient(connected=True)
    zone = FakeZone(TEST_ZONE_ID)
    client.add_zone(zone, ZoneType.FLOW_ZONE)

    harness = HeatPumpHarness(
        controller=None,  # type: ignore[arg-type]
        mqtt=mqtt,
        client=client,
        zone=zone,
        loop=loop,
    )

    async def wait_until_connected() -> None:
        harness.wait_connected_calls += 1

    harness.wait_until_connected = wait_until_connected
    controller = HeatPumpController(
        loop,
        mqtt,
        lambda: client,
        lambda: harness.authenticated,
        wait_until_connected,
    )
    harness.controller = controller
    # The constructor schedules the initial state via call_soon_threadsafe.
    await asyncio.sleep(0)

    yield harness

    # `harness.controller` may have been replaced by `restart()`.
    await harness.controller.stop()


@pytest.fixture
async def harness(heat_pump: HeatPumpHarness) -> HeatPumpHarness:
    """Short name for heat-pump tests."""
    return heat_pump


# --------------------------------------------------------------------------
# Pool-Controller
# --------------------------------------------------------------------------


@dataclass
class PoolHarness:
    controller: PoolController
    mqtt: FakeMqttBridge
    client: FakeGeckoClient
    loop: asyncio.AbstractEventLoop | None = None
    websession: FakeWebSession | None = None
    oauth_flow: FakeOAuthFlow | None = None

    def add_zone(self, zone: FakeZone, zone_type: Any = ZoneType.FLOW_ZONE) -> FakeZone:
        return self.client.add_zone(zone, zone_type)


@pytest.fixture
async def pool(monkeypatch: pytest.MonkeyPatch) -> PoolHarness:
    """Pool controller with an authenticated fake and an empty zone list."""
    loop = asyncio.get_running_loop()
    mqtt = FakeMqttBridge(base=TEST_BASE_TOPIC)
    controller = PoolController(loop, mqtt)
    controller.client = FakeGeckoClient(connected=True)
    controller.authenticated = True
    await asyncio.sleep(0)
    yield PoolHarness(controller=controller, mqtt=mqtt, client=controller.client, loop=loop)
    await controller.heat_pump.stop()
    if controller.websession is not None:
        await controller.websession.close()
