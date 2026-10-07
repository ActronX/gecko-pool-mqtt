"""Requirements R-GE and R-CM: connection, lifecycle, and commands.

The ``gecko-iot-client`` library is not tested. ``GeckoIotClient``,
``MqttTransporter``, and ``SidecarGeckoApi`` are replaced with fakes in startup
tests so they do not access the Gecko cloud.
"""

from __future__ import annotations

import asyncio
import ast
import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from gecko_iot_client import EventChannel, ZoneType

from app import pool_controller as pool_controller_module
from app.config import settings
from app.gecko_recovery_logging import redact_error
from app.oauth_flow import OAuthAuthenticationError
from app.pool_controller import PoolController

from .conftest import PoolHarness
from .fakes import FakeGeckoClient, FakeOAuthFlow, FakeZone


def dotted(node: ast.Attribute) -> str:
    """Renders ``a.b.c`` from an attribute node."""
    parts: list[str] = [node.attr]
    current: Any = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


class RecordingGeckoClient(FakeGeckoClient):
    """Gecko fake that records the calling thread."""

    connect_threads: list[dict[str, Any]] = []
    instances: list["RecordingGeckoClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.init_kwargs = kwargs
        self.idd = kwargs.get("idd")
        self.transporter = kwargs.get("transporter")
        self.config_timeout = kwargs.get("config_timeout")
        type(self).instances.append(self)

    def connect(self) -> None:
        thread = threading.current_thread()
        type(self).connect_threads.append(
            {
                "name": thread.name,
                "daemon": thread.daemon,
                "is_main_thread": thread is threading.main_thread(),
            }
        )
        super().connect()


class FakeApi:
    """Fake for ``SidecarGeckoApi``."""

    vessels: list[dict[str, Any]] = [{"vesselId": "1", "monitorId": "mon-1", "name": "Pool"}]
    account: tuple[str, dict[str, Any]] = ("acc-1", {"name": "Pool"})
    broker_url: str = "wss://broker.test/mqtt"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def async_discover_account(self) -> tuple[str, dict[str, Any]]:
        return self.account

    async def async_get_vessels(self, account_id: str) -> list[dict[str, Any]]:
        return self.vessels

    async def async_get_broker_url(self, monitor_id: str) -> str:
        return self.broker_url

    def make_sync_refresh_callback(self, loop: Any, on_auth_failure: Any = None) -> Any:
        return lambda monitor_id: self.broker_url


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replaces the library and sidecar with fakes."""
    RecordingGeckoClient.connect_threads = []
    RecordingGeckoClient.instances = []
    FakeApi.vessels = [{"vesselId": "1", "monitorId": "mon-1", "name": "Pool"}]
    monkeypatch.setattr(pool_controller_module, "GeckoIotClient", RecordingGeckoClient)
    monkeypatch.setattr(pool_controller_module, "MqttTransporter", lambda **kwargs: object())
    monkeypatch.setattr(pool_controller_module, "SidecarGeckoApi", FakeApi)
    return {"vessels": FakeApi.vessels}


def write_token_file() -> None:
    with open(settings.oauth_token_file, "w", encoding="utf-8") as handle:
        json.dump(
            {"access_token": "at", "refresh_token": "rt", "expires_at": 9_999_999_999.0},
            handle,
        )


def test_gecko_recovery_error_redacts_broker_url() -> None:
    error = RuntimeError("failed at mqtts://user:secret@broker.example/mqtt?token=abc")

    message = redact_error(error)

    assert "broker.example" not in message
    assert "secret" not in message
    assert message == "failed at <redacted-url>"


# --------------------------------------------------------------------------
# R-GE-01, R-GE-03 Connection setup in a thread
# --------------------------------------------------------------------------


async def test_tc_ge_01_connect_runs_in_daemon_thread(pool: PoolHarness, library) -> None:
    """R-GE-01: connect() runs in a daemon thread."""
    pool.controller.oauth_flow = FakeOAuthFlow()

    assert await pool.controller.start_gecko_client() is True

    assert RecordingGeckoClient.connect_threads, "connect was never called"
    record = RecordingGeckoClient.connect_threads[0]
    assert record["daemon"] is True
    assert record["is_main_thread"] is False


async def test_tc_ge_01_initial_start_has_no_recovery_transition(
    pool: PoolHarness, library, caplog: pytest.LogCaptureFixture
) -> None:
    pool.controller.oauth_flow = FakeOAuthFlow()

    await pool.controller.start_gecko_client()

    assert "Gecko recovery state transition" not in caplog.text


async def test_tc_ge_03_successful_connect_marks_authenticated(pool: PoolHarness, library) -> None:
    """R-GE-03: A successful connection publishes authenticated."""
    pool.controller.oauth_flow = FakeOAuthFlow()

    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)

    assert pool.controller.authenticated is True
    assert any(entry["status"] == "authenticated" for entry in pool.mqtt.auth_status)


async def test_tc_ge_01_client_uses_discovered_monitor(pool: PoolHarness, library) -> None:
    """R-GE-01: The monitor ID comes from vessel discovery."""
    pool.controller.oauth_flow = FakeOAuthFlow()

    await pool.controller.start_gecko_client()

    assert pool.controller.selected_monitor_id == "mon-1"
    assert isinstance(pool.controller.client, RecordingGeckoClient)
    assert pool.controller.client.idd == "mon-1"


# --------------------------------------------------------------------------
# R-GE-02 Connect-Backoff
# --------------------------------------------------------------------------


class BackoffStopEvent:
    """Counts waits and ends the loop after n attempts."""

    def __init__(self, stop_after: int = 3) -> None:
        self.waits: list[float | None] = []
        self._stop_after = stop_after

    def is_set(self) -> bool:
        return False

    def wait(self, delay: float | None = None) -> bool:
        self.waits.append(delay)
        return len(self.waits) >= self._stop_after

    def set(self) -> None:
        return None


async def test_tc_ge_02_connect_failures_back_off_exponentially(
    pool: PoolHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """R-GE-02: Connection failures produce 5s, 10s, and 20s backoff."""
    client = FakeGeckoClient(connected=False)
    client.connect = lambda: (_ for _ in ()).throw(RuntimeError("no network"))  # type: ignore[method-assign]
    stop_event = BackoffStopEvent(stop_after=3)
    pool.controller._stop_event = stop_event  # type: ignore[assignment]
    pool.controller._connect_generation = 1
    # start_gecko_client sets authenticated to False before starting the thread.
    pool.controller.authenticated = False

    pool.controller._connect_worker(client, 1)  # type: ignore[arg-type]

    assert stop_event.waits == [5.0, 10.0, 20.0]
    assert client.calls.count("disconnect") == 3
    assert pool.controller.authenticated is False
    assert "Gecko recovery connect failed generation=1 attempt=1" in caplog.text
    assert "retrying in 5s" in caplog.text


async def test_tc_ge_02_stale_generation_stops_worker(pool: PoolHarness) -> None:
    """R-GE-02: A stale generation ends the loop immediately."""
    client = FakeGeckoClient(connected=False)
    pool.controller._connect_generation = 7

    pool.controller._connect_worker(client, 3)  # type: ignore[arg-type]

    assert client.calls == []


# --------------------------------------------------------------------------
# R-GE-04 Generationen
# --------------------------------------------------------------------------


def test_tc_ge_04_stale_generation_cannot_authenticate(pool: PoolHarness) -> None:
    """R-GE-04: A stale generation must not authenticate."""
    controller = pool.controller
    controller._connect_generation = 5
    controller.authenticated = False
    controller.client = FakeGeckoClient(connected=True)

    controller._mark_connected(4)

    assert controller.authenticated is False
    assert pool.mqtt.auth_status == []


def test_tc_ge_04_current_generation_authenticates(pool: PoolHarness) -> None:
    """R-GE-04: The current generation authenticates."""
    controller = pool.controller
    controller._connect_generation = 5
    controller.authenticated = False
    controller.client = FakeGeckoClient(connected=True)

    controller._mark_connected(5)

    assert controller.authenticated is True
    assert controller.reauth_required is False


# --------------------------------------------------------------------------
# R-GE-05, R-GE-06 Autostart
# --------------------------------------------------------------------------


async def test_tc_ge_05_without_tokens_requests_login(pool: PoolHarness) -> None:
    """R-GE-05: Without tokens, a login challenge is published."""
    if os.path.exists(settings.oauth_token_file):
        os.remove(settings.oauth_token_file)

    result = await pool.controller.try_start_from_tokens()

    assert result is False
    assert pool.mqtt.challenges, "No challenge was published"
    assert pool.mqtt.auth_status[-1]["status"] == "login_required"
    assert pool.mqtt.auth_status[-1]["reason"] == "no persisted OAuth tokens"


async def test_tc_ge_06_oauth_error_during_autostart_marks_reauth(pool: PoolHarness) -> None:
    """R-GE-06: An OAuth error during autostart leads to reauth_required."""
    write_token_file()

    async def failing_start() -> bool:
        raise OAuthAuthenticationError("Refresh abgelehnt", 401)

    pool.controller.start_gecko_client = failing_start  # type: ignore[method-assign]

    result = await pool.controller.try_start_from_tokens()

    assert result is False
    assert pool.controller.reauth_required is True
    assert any(entry["status"] == "reauth_required" for entry in pool.mqtt.auth_status)
    assert pool.mqtt.challenges


# --------------------------------------------------------------------------
# R-GE-07 Reauth
# --------------------------------------------------------------------------


async def test_tc_ge_07_reauth_is_idempotent(pool: PoolHarness) -> None:
    """R-GE-07: A repeated reauth trigger changes the state only once."""
    pool.controller.oauth_flow = FakeOAuthFlow()
    generation_before = pool.controller._connect_generation

    await pool.controller._mark_reauth_required(OAuthAuthenticationError("abgelehnt", 401))
    generation_after_first = pool.controller._connect_generation
    await pool.controller._mark_reauth_required(OAuthAuthenticationError("abgelehnt", 401))

    assert pool.controller.reauth_required is True
    assert pool.controller.authenticated is False
    assert generation_after_first == generation_before + 1
    assert pool.controller._connect_generation == generation_after_first


async def test_tc_ge_07_reauth_publishes_connectivity_and_disconnects(pool: PoolHarness) -> None:
    """R-GE-07: Reauth disconnects the client and publishes connectivity."""
    pool.controller.oauth_flow = FakeOAuthFlow()

    await pool.controller._mark_reauth_required(OAuthAuthenticationError("abgelehnt", 401))

    assert pool.client.disconnected_count() == 1
    assert pool.mqtt.connectivity[-1]["reauth_required"] is True
    assert pool.mqtt.connectivity[-1]["auth_status"] == "reauth_required"
    assert pool.mqtt.connectivity[-1]["transport_connected"] is False


# --------------------------------------------------------------------------
# R-GE-08 Monitor-ID
# --------------------------------------------------------------------------


async def test_tc_ge_08_missing_monitor_id_raises(pool: PoolHarness, library) -> None:
    """R-GE-08: Without monitorId and vesselId, startup aborts."""
    FakeApi.vessels = [{"vesselId": "", "monitorId": "", "name": "Pool"}]
    pool.controller.oauth_flow = FakeOAuthFlow()

    with pytest.raises(RuntimeError, match="No monitorId"):
        await pool.controller.start_gecko_client()


async def test_tc_ge_08_falls_back_to_vessel_id(pool: PoolHarness, library) -> None:
    """R-GE-08: If monitorId is missing, vesselId is used."""
    FakeApi.vessels = [{"vesselId": "vessel-9", "monitorId": "", "name": "Pool"}]
    pool.controller.oauth_flow = FakeOAuthFlow()

    await pool.controller.start_gecko_client()

    assert pool.controller.selected_monitor_id == "vessel-9"


async def test_tc_ge_08_without_vessels_raises(pool: PoolHarness, library) -> None:
    """R-GE-08: Without vessels, there is nothing to connect to."""
    FakeApi.vessels = []
    pool.controller.oauth_flow = FakeOAuthFlow()

    with pytest.raises(RuntimeError, match="No vessels found"):
        await pool.controller.start_gecko_client()


async def test_tc_ge_11_worker_is_not_restarted_after_success(pool: PoolHarness, library) -> None:
    """R-GE-11: After the first connection, the retry thread ends permanently.

    Later connection loss is handled by the controller's separate recovery task.
    """
    pool.controller.oauth_flow = FakeOAuthFlow()

    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)

    assert pool.controller._connect_thread is not None
    assert not pool.controller._connect_thread.is_alive(), "The connection thread is still running"

    # A second start creates a new client and a new thread.
    first_thread = pool.controller._connect_thread
    await pool.controller.start_gecko_client()

    assert pool.controller._connect_thread is not first_thread


# --------------------------------------------------------------------------
# R-GE-13 Transport recovery
# --------------------------------------------------------------------------


def incomplete_connectivity() -> SimpleNamespace:
    return SimpleNamespace(to_dict=lambda: {"is_fully_connected": False})


async def test_tc_ge_13_incomplete_transport_rebuilds_once(
    pool: PoolHarness, library, caplog: pytest.LogCaptureFixture
) -> None:
    """R-GE-13: A persistent incomplete transport rebuilds the Gecko client."""
    pool.controller.oauth_flow = FakeOAuthFlow()
    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)
    stale_client = pool.controller.client
    assert stale_client is not None
    stale_client.is_connected = False

    stale_client.emit(EventChannel.CONNECTIVITY_UPDATE, incomplete_connectivity())
    stale_client.emit(EventChannel.CONNECTIVITY_UPDATE, incomplete_connectivity())
    await asyncio.sleep(0)

    recovery_task = pool.controller._recovery_task
    assert recovery_task is not None
    await asyncio.sleep(0.05)

    assert pool.controller.client is not stale_client
    assert stale_client.disconnected_count() == 1
    assert len(RecordingGeckoClient.instances) == 2
    assert pool.controller._recovery_task is None
    assert "Gecko recovery scheduled generation=1" in caplog.text
    assert "delay=0.01s" in caplog.text
    assert "Gecko recovery rebuild attempt=1 generation=1" in caplog.text


async def test_tc_ge_13_recovered_transport_skips_rebuild(
    pool: PoolHarness, library, caplog: pytest.LogCaptureFixture
) -> None:
    """R-GE-13: A self-healed transport does not cause an unnecessary rebuild."""
    caplog.set_level(logging.INFO)
    pool.controller.oauth_flow = FakeOAuthFlow()
    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)
    client = pool.controller.client
    assert client is not None
    client.is_connected = False

    client.emit(EventChannel.CONNECTIVITY_UPDATE, incomplete_connectivity())
    await asyncio.sleep(0)
    client.is_connected = True
    await asyncio.sleep(0.05)

    assert pool.controller.client is client
    assert len(RecordingGeckoClient.instances) == 1
    assert pool.controller._recovery_task is None
    assert "Gecko recovery aborted: transport self-healed" in caplog.text


async def test_tc_ge_13_superseding_start_cancels_recovery(pool: PoolHarness, library) -> None:
    """R-GE-13: A newer client start replaces the pending recovery task."""
    pool.controller.oauth_flow = FakeOAuthFlow()
    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)
    client = pool.controller.client
    assert client is not None
    client.is_connected = False

    client.emit(EventChannel.CONNECTIVITY_UPDATE, incomplete_connectivity())
    await asyncio.sleep(0)
    assert pool.controller._recovery_task is not None
    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)

    assert len(RecordingGeckoClient.instances) == 2
    assert pool.controller._recovery_task is None


async def test_tc_ge_13_transient_rebuild_failure_retries(
    pool: PoolHarness, library, caplog: pytest.LogCaptureFixture
) -> None:
    """R-GE-13: Rebuild setup failures retry without requesting login."""
    pool.controller.oauth_flow = FakeOAuthFlow()
    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)
    client = pool.controller.client
    assert client is not None
    client.is_connected = False
    original_start = pool.controller.start_gecko_client
    attempts = 0

    async def flaky_start(recovery: bool = False) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Gecko API temporarily unavailable")
        return await original_start(recovery=recovery)

    pool.controller.start_gecko_client = flaky_start  # type: ignore[method-assign]
    client.emit(EventChannel.CONNECTIVITY_UPDATE, incomplete_connectivity())
    await asyncio.sleep(0.1)

    assert attempts == 2
    assert pool.controller.authenticated is True
    assert pool.controller.reauth_required is False
    assert not any(status["status"] == "login_required" for status in pool.mqtt.auth_status)
    assert "Gecko recovery rebuild failed attempt=1" in caplog.text
    assert "next attempt in 0.02s" in caplog.text


async def test_tc_ge_13_oauth_failure_requires_reauthentication(
    pool: PoolHarness, library, caplog: pytest.LogCaptureFixture
) -> None:
    """R-GE-13: An authentication failure during rebuild requires a new login."""
    pool.controller.oauth_flow = FakeOAuthFlow()
    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)
    client = pool.controller.client
    assert client is not None
    client.is_connected = False

    async def failing_start(recovery: bool = False) -> bool:
        raise OAuthAuthenticationError("refresh rejected", 401)

    pool.controller.start_gecko_client = failing_start  # type: ignore[method-assign]
    client.emit(EventChannel.CONNECTIVITY_UPDATE, incomplete_connectivity())
    await asyncio.sleep(0.05)

    assert pool.controller.reauth_required is True
    assert pool.controller.authenticated is False
    assert client.disconnected_count() == 1
    assert pool.mqtt.challenges
    assert "Gecko recovery aborted: reauth_required" in caplog.text


async def test_tc_ge_13_stop_cancels_pending_recovery(
    pool: PoolHarness, library, caplog: pytest.LogCaptureFixture
) -> None:
    """R-GE-13: Shutdown prevents a delayed recovery from recreating a client."""
    caplog.set_level(logging.INFO)
    pool.controller.oauth_flow = FakeOAuthFlow()
    await pool.controller.start_gecko_client()
    await asyncio.sleep(0.05)
    client = pool.controller.client
    assert client is not None
    client.is_connected = False

    client.emit(EventChannel.CONNECTIVITY_UPDATE, incomplete_connectivity())
    await asyncio.sleep(0)
    await pool.controller.stop()
    await asyncio.sleep(0.05)

    assert len(RecordingGeckoClient.instances) == 1
    assert pool.controller._recovery_task is None
    assert "Gecko recovery aborted: shutdown" in caplog.text


async def test_tc_ge_12_transporter_gets_a_token_refresh_callback(
    pool: PoolHarness, library, monkeypatch
) -> None:
    """R-GE-12: The bridge passes a token_refresh_callback.

    Without this callback, the transporter does not schedule a reconnect after
    connection loss because `_handle_connection_lost` requires it.
    """
    seen: dict[str, Any] = {}

    class RecordingApi(FakeApi):
        def make_sync_refresh_callback(self, loop: Any, on_auth_failure: Any = None) -> Any:
            seen["loop"] = loop
            seen["on_auth_failure"] = on_auth_failure
            return lambda monitor_id: "wss://neu.test/mqtt"

    monkeypatch.setattr(pool_controller_module, "SidecarGeckoApi", RecordingApi)
    pool.controller.oauth_flow = FakeOAuthFlow()

    await pool.controller.start_gecko_client()

    assert seen["on_auth_failure"] == pool.controller._mark_reauth_required
    assert seen["loop"] is pool.controller.loop


# --------------------------------------------------------------------------
# R-GE-09, R-GE-10 Wartebedingung
# --------------------------------------------------------------------------


async def test_tc_ge_09_missing_client_raises_immediately(pool: PoolHarness) -> None:
    """R-GE-09: Without a client, no silent success is reported."""
    pool.controller.client = None

    with pytest.raises(RuntimeError, match="not available"):
        await pool.controller._wait_until_connected()


async def test_tc_ge_10_wait_times_out(pool: PoolHarness) -> None:
    """R-GE-10: Without a connection, the timeout raises an error."""
    pool.client.is_connected = False

    with pytest.raises(RuntimeError, match="not fully connected"):
        await pool.controller._wait_until_connected()


async def test_tc_ge_10_connected_client_returns_immediately(pool: PoolHarness) -> None:
    """R-GE-10: A connected client returns immediately."""
    pool.client.is_connected = True

    await pool.controller._wait_until_connected()


# --------------------------------------------------------------------------
# R-CM Commands
# --------------------------------------------------------------------------


async def test_tc_cm_01_unauthenticated_command_fails(pool: PoolHarness) -> None:
    """R-CM-01: No command runs without authentication."""
    pool.controller.authenticated = False

    await pool.controller._handle_command("flow", "1", {"action": "on"})

    assert pool.mqtt.results[-1]["success"] is False
    assert "not authenticated" in pool.mqtt.results[-1]["message"]


async def test_tc_cm_01_missing_client_fails(pool: PoolHarness) -> None:
    """R-CM-01: No command runs without a client."""
    pool.controller.client = None

    await pool.controller._handle_command("flow", "1", {"action": "on"})

    assert pool.mqtt.results[-1]["success"] is False


async def test_tc_cm_02_temperature_command(pool: PoolHarness) -> None:
    """R-CM-02: A temperature command sets the target value."""
    zone = pool.add_zone(FakeZone("zone-1"), ZoneType.TEMPERATURE_CONTROL_ZONE)

    await pool.controller._handle_command("temperature", "zone-1", {"target_temperature": 28.0})

    assert zone.last_args("set_target_temperature") == ((28.0,), {})
    assert pool.mqtt.results[-1] == {
        "command_type": "temperature",
        "zone_id": "zone-1",
        "success": True,
        "message": "Target temperature set to 28.0",
    }


async def test_tc_cm_03_lighting_off(pool: PoolHarness) -> None:
    """R-CM-03: Turning the light off deactivates the zone."""
    zone = pool.add_zone(FakeZone("l1"), ZoneType.LIGHTING_ZONE)

    await pool.controller._handle_command("lighting", "l1", {"action": "off"})

    assert zone.call_names() == ["deactivate"]


async def test_tc_cm_03_lighting_effect(pool: PoolHarness) -> None:
    """R-CM-03: An effect takes precedence over the color."""
    zone = pool.add_zone(FakeZone("l1"), ZoneType.LIGHTING_ZONE)

    await pool.controller._handle_command("lighting", "l1", {"action": "on", "effect": "rainbow"})

    assert zone.last_args("set_effect") == (("rainbow",), {})


async def test_tc_cm_03_lighting_colour(pool: PoolHarness) -> None:
    """R-CM-03: Without an effect, the color is set."""
    zone = pool.add_zone(FakeZone("l1"), ZoneType.LIGHTING_ZONE)

    await pool.controller._handle_command(
        "lighting", "l1", {"action": "on", "r": 255, "g": 120, "b": 40, "intensity": 200}
    )

    assert zone.last_args("set_color") == ((255, 120, 40, 200), {})


async def test_tc_cm_05_flow_on_without_speed(pool: PoolHarness) -> None:
    """R-CM-05: Flow on without speed activates the zone."""
    zone = pool.add_zone(FakeZone("1"), ZoneType.FLOW_ZONE)

    await pool.controller._handle_command("flow", "1", {"action": "on"})

    assert zone.call_names() == ["activate"]
    assert pool.mqtt.results[-1]["success"] is True


async def test_tc_cm_05_flow_on_with_speed(pool: PoolHarness) -> None:
    """R-CM-05: Flow on with speed sets the speed."""
    zone = pool.add_zone(
        FakeZone("1", speed_config={"minimum": 20, "maximum": 100, "stepIncrement": 10}),
        ZoneType.FLOW_ZONE,
    )

    await pool.controller._handle_command("flow", "1", {"action": "on", "speed": 50})

    assert zone.last_args("set_speed") == ((50,), {"active": True})


async def test_tc_cm_05_flow_off(pool: PoolHarness) -> None:
    """R-CM-05: Flow off deactivates the zone."""
    zone = pool.add_zone(FakeZone("1"), ZoneType.FLOW_ZONE)

    await pool.controller._handle_command("flow", "1", {"action": "off"})

    assert zone.call_names() == ["deactivate"]


@pytest.mark.parametrize("speed_config", [None, {"stepIncrement": 0}])
async def test_tc_cm_04_speed_on_fixed_speed_pump_fails(pool: PoolHarness, speed_config) -> None:
    """R-CM-04: An on/off pump rejects speed."""
    pool.add_zone(FakeZone("1", speed_config=speed_config), ZoneType.FLOW_ZONE)

    await pool.controller._handle_command("flow", "1", {"action": "on", "speed": 50})

    result = pool.mqtt.results[-1]
    assert result["success"] is False
    assert result["message"] == (
        "Flow zone 1 supports on/off only; speed percentage is not supported"
    )


async def test_tc_cm_06_unknown_command_type_fails(pool: PoolHarness) -> None:
    """R-CM-06: An unknown command type produces an error result."""
    await pool.controller._handle_command("steam", "1", {})

    assert pool.mqtt.results[-1]["success"] is False
    assert "unsupported command type: steam" in pool.mqtt.results[-1]["message"]


async def test_tc_cm_07_gecko_exception_becomes_error_result(pool: PoolHarness) -> None:
    """R-CM-07: A library exception is reported as an error."""
    zone = pool.add_zone(FakeZone("1"), ZoneType.FLOW_ZONE)
    zone.fail("activate", RuntimeError("PUBACK missing"))

    await pool.controller._handle_command("flow", "1", {"action": "on"})

    result = pool.mqtt.results[-1]
    assert result["success"] is False
    assert "PUBACK missing" in result["message"]


async def test_tc_cm_07_unknown_zone_becomes_error_result(pool: PoolHarness) -> None:
    """R-CM-07: An unknown zone produces an error result without a crash."""
    await pool.controller._handle_command("flow", "99", {"action": "on"})

    assert pool.mqtt.results[-1]["success"] is False


# --------------------------------------------------------------------------
# R-CM-08 Callback extraction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_code", "expected_state"),
    [
        ({"code": "abc", "state": "s1"}, "abc", "s1"),
        ({"redirect_url": "https://x.test/redirect?code=def&state=s2"}, "def", "s2"),
         # Home Assistant wraps the callback in a redirect parameter.
         # In practice, it arrives percent-encoded.
        (
            {"redirect_url": "https://x.test/redirect?redirect=%2Fcallback%3Fcode%3Dghi%26state%3Ds3"},
            "ghi",
            "s3",
        ),
    ],
)
def test_tc_cm_08_callback_values_are_extracted(payload, expected_code, expected_state) -> None:
    """R-CM-08: Code and state are read from all supported forms."""
    code, state = PoolController._extract_callback_values(payload)

    assert code == expected_code
    assert state == expected_state


def test_tc_cm_08_redirect_url_without_code_raises() -> None:
    """R-CM-08: A redirect_url without a code is rejected."""
    with pytest.raises(ValueError, match="No OAuth code"):
        PoolController._extract_callback_values({"redirect_url": "https://x.test/redirect"})


@pytest.mark.parametrize("payload", [{}, {"redirect_url": "https://x.test/redirect"}, {"other": 1}])
def test_tc_cm_08_missing_code_raises(payload) -> None:
    """R-CM-08: Without a code and a complete URL, an error is raised."""
    with pytest.raises(ValueError):
        PoolController._extract_callback_values(payload)


# --------------------------------------------------------------------------
# R-MQ-11 Invalid auth response
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["", "   ", "\n"])
def test_tc_mq_11_empty_auth_response_is_ignored(pool: PoolHarness, payload: str) -> None:
    """R-MQ-11: An empty auth response is ignored instead of failing the session.

    Production finding: an empty payload on `auth/response` was parsed as a
    login attempt without a code, and the bridge answered `login_required` even
    though the tokens were valid. That answer then stuck, because nothing
    republishes the auth status outside a connect.
    """
    pool.controller.authenticated = True

    pool.controller.handle_auth_response(payload)

    assert pool.mqtt.auth_status == []
    assert pool.mqtt.challenges == []
    assert pool.controller.authenticated is True


def test_tc_mq_11_invalid_response_keeps_valid_session_authenticated(pool: PoolHarness) -> None:
    """R-MQ-11: An invalid response does not downgrade a valid session.

    A garbage payload is a diagnostic event, not a login request. Reporting
    `login_required` would contradict `connectivity`, which still reports
    `authenticated`.
    """
    pool.controller.authenticated = True

    pool.controller.handle_auth_response("https://example.test/redirect?state=only")

    assert pool.controller.authenticated is True
    status = pool.mqtt.auth_status[-1]
    assert status["status"] == "authenticated"
    assert "invalid response ignored" in status["reason"]


def test_tc_mq_11_invalid_response_without_session_reports_login_required(
    pool: PoolHarness,
) -> None:
    """R-MQ-11: Without a valid session an invalid response reports login_required."""
    pool.controller.authenticated = False

    pool.controller.handle_auth_response("https://example.test/redirect?state=only")

    status = pool.mqtt.auth_status[-1]
    assert status["status"] == "login_required"
    assert "No OAuth code" in status["reason"]


def test_tc_mq_11_reason_is_truncated(pool: PoolHarness) -> None:
    """R-MQ-11: The reason stays within the documented 240 characters."""
    pool.controller.authenticated = True

    pool.controller.handle_auth_response("https://example.test/redirect?state=" + "x" * 400)

    assert len(pool.mqtt.auth_status[-1]["reason"]) <= 240


# --------------------------------------------------------------------------
# R-OPS-02 Shutdown order
# --------------------------------------------------------------------------


def _call_of(statement: ast.stmt) -> ast.Call | None:
    """Unwraps Expr and Await nodes and returns the call behind them."""
    node: Any = statement
    if isinstance(node, ast.Expr):
        node = node.value
    if isinstance(node, ast.Await):
        node = node.value
    return node if isinstance(node, ast.Call) else None


def test_tc_ops_02_controller_stops_before_bridge() -> None:
    """R-OPS-02: During shutdown, the controller stops before the bridge."""
    source = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    finalbody_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for statement in node.finalbody:
            call = _call_of(statement)
            if call is not None and isinstance(call.func, ast.Attribute):
                finalbody_calls.append(dotted(call.func))

    assert finalbody_calls == ["controller.stop", "bridge.stop"]
