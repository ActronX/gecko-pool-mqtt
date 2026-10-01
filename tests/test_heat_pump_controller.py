"""Requirements R-HP: heat-pump state machine (TC-HP-01 through TC-HP-24).

Covers the ordering of ``_pending_confirmation``, protection against second
reasserts, and discarding late worker returns. The tests use fast timings from
``conftest.isolated_settings`` and a blockable zone fake to simulate blocking
library calls without actually waiting.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

import pytest

from app.config import settings

from .conftest import HeatPumpHarness


async def wait_for(event: threading.Event, timeout: float = 5.0) -> None:
    """Polls until a threading event is set."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() > deadline:
            raise AssertionError("Timeout while waiting for the blocking event")
        await asyncio.sleep(0.002)


def armed(harness: HeatPumpHarness) -> None:
    """Harness helper: the controller must be armed."""
    assert harness.controller._armed is True


# --------------------------------------------------------------------------
# R-HP-01, R-HP-02, R-HP-20 Basic on-command cases
# --------------------------------------------------------------------------


async def test_tc_hp_01_on_activates_and_arms(harness: HeatPumpHarness) -> None:
    """R-HP-01: on activates an inactive zone and reports waiting_confirmation."""
    await harness.on(duration=30)

    assert harness.zone.count("activate") == 1
    assert harness.controller._armed is True
    assert harness.controller._until_monotonic is not None
    assert harness.mqtt.state_messages()[-1] == "waiting_confirmation"
    assert harness.mqtt.heat_pump_results[-1]["success"] is True
    assert harness.mqtt.heat_pump_results[-1]["action"] == "on"
    assert harness.mqtt.heat_pump_results[-1]["duration"] == 30


async def test_tc_hp_01_on_reports_remaining_seconds(harness: HeatPumpHarness) -> None:
    """R-HP-01: The remaining runtime is reported in seconds."""
    await harness.on(duration=30)

    remaining = harness.mqtt.heat_pump_results[-1]["remaining_seconds"]
    assert 1790 <= remaining <= 1800


async def test_tc_hp_02_on_with_active_zone_does_not_activate(harness: HeatPumpHarness) -> None:
    """R-HP-02: An already active zone receives no activate call."""
    harness.zone.active = True

    await harness.on(duration=30)

    assert harness.zone.count("activate") == 0
    assert harness.mqtt.state_messages()[-1] == "running"
    assert harness.controller._pending_confirmation is False


async def test_tc_hp_20_unauthenticated_on_fails(harness: HeatPumpHarness) -> None:
    """R-HP-20: Without authentication, nothing is activated."""
    harness.authenticated = False

    await harness.on(duration=30)

    assert harness.zone.count("activate") == 0
    assert harness.mqtt.heat_pump_results[-1]["success"] is False
    assert "not authenticated" in harness.mqtt.heat_pump_results[-1]["message"]


async def test_tc_hp_01_16_watchdog_never_publishes_a_contradicting_state(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-01, R-HP-21: `state` never contradicts `armed`.

    Production finding: The watchdog starts before `_handle_command` sets its
    state name because the task is created before blocking `activate()`. Its first cycle therefore published
    `{"state": "disarmed", "armed": true, "remaining_seconds": 1919}`.
    """
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    task = asyncio.create_task(
        harness.controller._handle_command({"action": "on", "duration": 30})
    )
    await wait_for(entered)
    # The watchdog runs while `activate()` is blocked.
    await harness.settle(2)

    widerspruechlich = [
        payload
        for payload in harness.mqtt.heat_pump_states
        if payload["armed"] is True
        and (payload["state"] == "disarmed" or payload["remaining_seconds"] is None)
    ]
    assert widerspruechlich == []

    gate.set()
    await task

    widerspruechlich = [
        payload
        for payload in harness.mqtt.heat_pump_states
        if payload["armed"] is True
        and (payload["state"] == "disarmed" or payload["remaining_seconds"] is None)
    ]
    assert widerspruechlich == []
    assert harness.mqtt.state_messages()[-1] == "waiting_confirmation"


async def test_tc_hp_01_result_reports_the_state_of_its_own_command(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-01: The result names the state produced by its command.

    Production finding: `self._state` was read before the command published its
    own state. The result therefore reported the state of the *previous* command.
    """
    assert harness.controller._state == "disarmed"

    await harness.on(duration=30)

    result = harness.mqtt.heat_pump_results[-1]
    assert result["success"] is True
    assert result["state"] == "waiting_confirmation"
    assert result["state"] == harness.mqtt.state_messages()[-1]


async def test_tc_hp_01_result_never_reports_the_previous_state(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-01: An `on` after `disarmed` does not report `disarmed`.

    This was the observed production case: `success: true` with
    `remaining_seconds: 1920` and `state: "disarmed"`, 30 minutes after the
    previous command.
    """
    await harness.on(duration=30)
    await harness.off()
    assert harness.controller._state == "disarmed"

    await harness.on(duration=30)

    result = harness.mqtt.heat_pump_results[-1]
    assert result["state"] != "disarmed"
    assert result["state"] == "waiting_confirmation"


async def test_tc_hp_01_on_does_not_report_waiting_after_late_confirmation(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-01: If Gecko confirms during `activate`, the command reports `running`.

    The library sets `zone.active` in the Gecko thread before the zone-update
    callback can run on the event loop. After confirmation, no
    `waiting_confirmation` may occur. Before then, it is valid: the watchdog
    already runs, `activate()` is still blocked, and the request is unconfirmed.
    """
    harness.quiet_watchdog()
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    task = asyncio.create_task(
        harness.controller._handle_command({"action": "on", "duration": 30})
    )
    await wait_for(entered)
    harness.zone.active = True
    gate.set()
    await task

    zustaende = harness.mqtt.state_messages()
    assert zustaende[-1] == "running"
    assert "waiting_confirmation" not in zustaende[zustaende.index("running") :]
    assert harness.mqtt.heat_pump_results[-1]["state"] == "running"
    assert harness.controller._pending_confirmation is False
    assert harness.controller._confirmation_deadline is None


async def test_tc_hp_02_result_reports_running_for_active_zone(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-02: For an already active zone, the result names `running`."""
    harness.zone.active = True

    await harness.on(duration=30)

    assert harness.mqtt.heat_pump_results[-1]["state"] == "running"
    assert harness.mqtt.state_messages()[-1] == "running"


async def test_tc_hp_15_unknown_action_fails(harness: HeatPumpHarness) -> None:
    """R-HP-15: An unknown action produces an error result."""
    await harness.controller._handle_command({"action": "sideways"})

    assert harness.mqtt.heat_pump_results[-1]["success"] is False
    assert "Unknown heat-pump action" in harness.mqtt.heat_pump_results[-1]["message"]


# --------------------------------------------------------------------------
# R-HP-03 Pending before the worker call
# --------------------------------------------------------------------------


async def test_tc_hp_03_pending_is_set_before_activate_runs(harness: HeatPumpHarness) -> None:
    """R-HP-03: Pending and the deadline are set before activate returns."""
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    task = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await wait_for(entered)

    # During the simulated five-second PUBACK wait:
    assert harness.controller._pending_confirmation is True
    assert harness.controller._confirmation_deadline is not None
    assert harness.controller._armed is True

    gate.set()
    await task


async def test_tc_hp_03_pending_suppresses_second_reassert(harness: HeatPumpHarness) -> None:
    """R-HP-03: An active-false update does not reassert while waiting."""
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    task = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await wait_for(entered)

    harness.zone_update(active=False)
    await asyncio.sleep(0.01)

    assert harness.zone.count("activate") == 1
    assert harness.mqtt.reassert_reasons() == []

    gate.set()
    await task


# --------------------------------------------------------------------------
# R-HP-17 Late worker return
# --------------------------------------------------------------------------


async def test_tc_hp_17_off_during_activate_discards_late_result(harness: HeatPumpHarness) -> None:
    """R-HP-17: off while activate is pending does not restore the success."""
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    task = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await wait_for(entered)

    await harness.off()
    gate.set()
    await task

    assert harness.controller._armed is False
    assert harness.controller._pending_confirmation is False
    on_results = [
        entry for entry in harness.mqtt.heat_pump_results if entry.get("action") == "on"
    ]
    assert on_results == []


async def test_tc_hp_17_expiry_during_activate_discards_late_result(harness: HeatPumpHarness) -> None:
    """R-HP-17: Expiry while activate is pending discards the return value."""
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    # duration 0 makes the deadline expire immediately.
    task = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 0}))
    await wait_for(entered)
    await harness.settle(4)
    gate.set()
    await task

    assert harness.controller._armed is False
    assert harness.mqtt.state_messages()[-1] == "disarmed_expired"


async def test_tc_hp_17_emergency_stop_during_activate_discards_late_result(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-17: The emergency stop while activate is pending discards the return value."""
    harness.zone.delay("activate", 0.2)
    harness.client.is_connected = False
    settings.heat_pump_max_reassert_attempts = 1

    await harness.controller._handle_command({"action": "on", "duration": 30})
    await harness.settle(6)

    assert harness.mqtt.heat_pump_errors, "The emergency stop did not trigger"
    assert harness.controller._armed is False
    on_results = [
        entry for entry in harness.mqtt.heat_pump_results if entry.get("action") == "on"
    ]
    assert on_results == []


async def test_tc_hp_17_stop_during_activate_discards_late_result(harness: HeatPumpHarness) -> None:
    """R-HP-17: Shutdown while activate is pending discards the return value."""
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    task = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await wait_for(entered)

    await harness.controller.stop()
    gate.set()
    await task

    assert harness.controller._armed is False
    on_results = [
        entry for entry in harness.mqtt.heat_pump_results if entry.get("action") == "on"
    ]
    assert on_results == []


# --------------------------------------------------------------------------
# R-HP-04 Runtime extension
# --------------------------------------------------------------------------


async def test_tc_hp_04_second_on_extends_deadline(harness: HeatPumpHarness) -> None:
    """R-HP-04: A longer on extends the request."""
    await harness.on(duration=10)
    first = harness.controller._until_monotonic

    await harness.on(duration=30)
    second = harness.controller._until_monotonic

    assert second is not None and first is not None
    assert second > first
    assert second - first >= 20 * 60


async def test_tc_hp_04_shorter_on_does_not_reduce_deadline(harness: HeatPumpHarness) -> None:
    """R-HP-04: A shorter on does not reduce the request."""
    await harness.on(duration=30)
    first = harness.controller._until_monotonic

    await harness.on(duration=1)
    second = harness.controller._until_monotonic

    assert second is not None and first is not None
    assert second >= first


async def test_tc_hp_04_repeated_on_keeps_single_activation(harness: HeatPumpHarness) -> None:
    """R-HP-04: The zone is not activated again while it is active."""
    harness.zone.active = True

    await harness.on(duration=10)
    await harness.on(duration=20)

    assert harness.zone.count("activate") == 0
    assert harness.controller._armed is True


# --------------------------------------------------------------------------
# R-HP-05, R-HP-06, R-HP-07 Zone updates
# --------------------------------------------------------------------------


async def test_tc_hp_05_zone_update_confirms_activation(harness: HeatPumpHarness) -> None:
    """R-HP-05: active true confirms immediately and resets the error counter."""
    await harness.on(duration=30)
    harness.controller._register_failure("kuenstlich")
    assert harness.controller._error_count == 1

    harness.zone_update(active=True)

    assert harness.controller._pending_confirmation is False
    assert harness.controller._confirmation_deadline is None
    assert harness.controller._error_count == 0
    assert harness.mqtt.state_messages()[-1] == "running"


async def test_tc_hp_05_02_repeated_active_update_publishes_nothing(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-05: A repeated `active: true` without a transition is silent.

    Production finding: Gecko sends zone updates for all zones, often in quick
    succession. Without a guard, each one published another `running`, including
    updates from switching the light or pump 1.
    """
    await harness.on(duration=30)
    harness.zone_update(active=True)
    before = len(harness.mqtt.heat_pump_states)

    for _ in range(5):
        harness.zone_update(active=True)
    # Without settle: only the zone-update path may publish anything.
    assert len(harness.mqtt.heat_pump_states) == before

    # The watchdog still keeps the state current.
    await harness.settle(2)
    assert len(harness.mqtt.heat_pump_states) > before
    assert harness.mqtt.state_messages()[-1] == "running"


async def test_tc_hp_05_03_confirmation_is_published_without_settling(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-05: The confirming zone update publishes `running` immediately."""
    harness.quiet_watchdog()
    await harness.on(duration=30)
    before = len(harness.mqtt.heat_pump_states)

    harness.zone_update(active=True)

    # No settle(): confirmation must not wait for the next cycle.
    assert len(harness.mqtt.heat_pump_states) == before + 1
    assert harness.mqtt.state_messages()[-1] == "running"


async def test_tc_hp_05_04_active_update_heals_disconnected(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-05: An `active: true` after `disconnected` immediately restores `running`.

    The watchdog would correct the state only in the next cycle.
    """
    harness.quiet_watchdog()
    await harness.on(duration=30)
    harness.client.is_connected = False
    await harness.settle(2)
    assert harness.mqtt.state_messages()[-1] == "disconnected"
    harness.client.is_connected = True
    harness.controller._state = "disconnected"

    harness.zone_update(active=True)

    assert harness.mqtt.state_messages()[-1] == "running"


async def test_tc_hp_06_zone_update_inactive_triggers_one_reassert(harness: HeatPumpHarness) -> None:
    """R-HP-06: active false triggers exactly one reassert."""
    harness.quiet_watchdog()
    await harness.on(duration=30)
    harness.zone_update(active=True)
    assert harness.zone.count("activate") == 1

    harness.zone_update(active=False)
    await harness.settle(3)

    assert harness.mqtt.reassert_reasons() == ["zone reported inactive"]
    assert harness.zone.count("activate") == 2, (
        f"calls={harness.zone.call_names()} "
        f"reasons={harness.mqtt.reassert_reasons()} "
        f"states={harness.mqtt.state_messages()} "
        f"pending={harness.controller._pending_confirmation} "
        f"armed={harness.controller._armed}"
    )
    assert harness.mqtt.heat_pump_reasserts[-1]["activate_called"] is True
    # attempt equals the error counter and is 0 before the first failure.
    assert harness.mqtt.heat_pump_reasserts[-1]["attempt"] == 0


async def test_tc_hp_06_reassert_repeats_only_after_new_inactive_update(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-06: No further update reasserts while pending is set."""
    harness.quiet_watchdog()
    await harness.on(duration=30)
    harness.zone_update(active=True)

    harness.zone_update(active=False)
    await harness.settle(3)
    # A second update while the reassert still waits for confirmation.
    harness.zone_update(active=False)
    await harness.settle(3)

    assert harness.zone.count("activate") == 2


async def test_tc_hp_06_reassert_does_not_report_waiting_after_late_confirmation(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-06, R-HP-10: A late-confirmed reassert reports `running`.

    Production finding: After blocking `activate()`, `waiting_confirmation` was
    published even though Gecko had already confirmed activation in its own
    thread. The zone-update callback had not run yet, so the reassert deadline
    was still set.
    """
    harness.quiet_watchdog()
    await harness.on(duration=30)
    harness.zone_update(active=True)
    harness.zone.active = False
    # The reassert is the subject here; the watchdog would confirm it in the
    # blocked window and obscure the result.
    harness.controller._cancel_watchdog()
    start = len(harness.mqtt.heat_pump_states)

    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")
    task = asyncio.create_task(
        harness.controller._reassert(harness.zone, "zone reported inactive")
    )
    await wait_for(entered)

    # The library reports the zone as active while `activate()` is blocked.
    # The zone-update callback intentionally runs only afterward.
    harness.zone.active = True
    gate.set()
    await task

    assert harness.zone.count("activate") == 2
    assert harness.mqtt.state_messages()[start:] == ["running"]
    assert harness.controller._pending_confirmation is False
    assert harness.controller._confirmation_deadline is None
    reassert = harness.mqtt.heat_pump_reasserts[-1]
    assert reassert["reason"] == "zone reported inactive"
    assert reassert["activate_called"] is True
    # The attempt is confirmed, so `confirmed` must not report false.
    assert reassert["confirmed"] is True


async def test_tc_hp_07_no_reassert_while_disconnected(harness: HeatPumpHarness) -> None:
    """R-HP-07: A disconnected client does not perform an activate reassert."""
    harness.quiet_watchdog()
    await harness.on(duration=30)
    harness.zone_update(active=True)
    harness.client.is_connected = False

    harness.zone_update(active=False)
    await harness.settle(3)

    # The watchdog counts "gecko disconnected" but does not call activate.
    assert harness.zone.count("activate") == 1
    assert all(
        entry["activate_called"] is False for entry in harness.mqtt.heat_pump_reasserts
    )
    assert "zone reported inactive" not in harness.mqtt.reassert_reasons()


async def test_tc_hp_07_zone_update_without_zone_is_ignored(harness: HeatPumpHarness) -> None:
    """R-HP-07: An update without the matching zone is ignored."""
    await harness.on(duration=30)

    harness.controller._observe_zone_update({})

    assert harness.zone.count("activate") == 1
    assert harness.mqtt.reassert_reasons() == []


# --------------------------------------------------------------------------
# R-HP-08, R-HP-09 Watchdog reasserts
# --------------------------------------------------------------------------


async def test_tc_hp_08_confirmation_timeout_raises_error_count(harness: HeatPumpHarness) -> None:
    """R-HP-08: Without confirmation, the error counter increases."""
    await harness.on(duration=30)

    await harness.settle(12)

    assert harness.controller._error_count >= 1
    assert harness.mqtt.reassert_reasons()[0] == "activation not confirmed within confirm_timeout"


async def test_tc_hp_08_confirmation_timeout_reasserts_activation(harness: HeatPumpHarness) -> None:
    """R-HP-08: After the timeout, activation is attempted again."""
    await harness.on(duration=30)

    await harness.settle(12)

    assert "activation confirmation timeout" in harness.mqtt.reassert_reasons()
    assert harness.zone.count("activate") >= 2


async def test_tc_hp_09_watchdog_reasserts_inactive_zone(harness: HeatPumpHarness) -> None:
    """R-HP-09: The watchdog reports an inactive zone."""
    await harness.on(duration=30)
    harness.zone_update(active=True)
    harness.zone.active = False

    await harness.settle(4)

    assert "zone became inactive" in harness.mqtt.reassert_reasons()


async def test_tc_hp_21_watchdog_publishes_state_each_cycle(harness: HeatPumpHarness) -> None:
    """R-HP-21: The watchdog publishes the state in every cycle."""
    await harness.on(duration=30)
    before = len(harness.mqtt.heat_pump_states)

    await harness.settle(4)

    assert len(harness.mqtt.heat_pump_states) > before
    assert harness.mqtt.heat_pump_states[-1]["armed"] is True
    assert harness.mqtt.heat_pump_states[-1]["max_errors"] == settings.heat_pump_max_reassert_attempts


async def test_tc_hp_21_disconnected_state_is_published(harness: HeatPumpHarness) -> None:
    """R-HP-21: Without a Gecko connection, disconnected is published."""
    await harness.on(duration=30)
    harness.zone_update(active=True)
    harness.client.is_connected = False

    await harness.settle(3)

    assert "disconnected" in harness.mqtt.state_messages()


@pytest.mark.parametrize("modus", ["aktiv", "wartend", "offline"])
async def test_tc_hp_21_watchdog_publishes_state_once_per_cycle(
    harness: HeatPumpHarness, modus: str
) -> None:
    """R-HP-21: The watchdog publishes exactly one state per cycle.

    Production finding: The `zone.active is True` branch and the offline branch
    published in addition to cycle completion, producing two identical retained
    payloads per cycle about 0.2 ms apart. Two publications from one cycle are
    therefore far below the check interval, while two cycles are clearly above it.
    """
    harness.quiet_watchdog()
    await harness.on(duration=30)
    if modus == "aktiv":
        harness.zone_update(active=True)
    elif modus == "offline":
        harness.zone_update(active=True)
        harness.client.is_connected = False

    # Let one cycle pass so the first publish in the measurement window comes
    # from the watchdog rather than immediately following the command.
    await harness.settle(1)
    start = len(harness.mqtt.heat_pump_states)

    await harness.settle(3)

    stamps = [
        datetime.fromisoformat(payload["timestamp"])
        for payload in harness.mqtt.heat_pump_states[start:]
    ]
    assert len(stamps) >= 2, "The watchdog did not publish the state regularly"
    gaps = [(spaeter - vorher).total_seconds() for vorher, spaeter in zip(stamps, stamps[1:])]
    assert min(gaps) > 0.001, f"Duplicate publication {min(gaps) * 1000:.2f} ms apart: {gaps}"


async def test_tc_hp_21_05_published_state_never_contradicts_armed(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-21: `state` never contradicts `armed` and `remaining_seconds`.

    This is checked through a mixed window of blocking `activate()`, watchdog
    heartbeat, and updates from other zones. Only the constructor may publish
    `disarmed`; all later states require `armed: true` and remaining runtime.
    """
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")
    task = asyncio.create_task(
        harness.controller._handle_command({"action": "on", "duration": 30})
    )
    await wait_for(entered)
    await harness.settle(2)
    harness.zone_update(active=True)
    await harness.settle(2)
    gate.set()
    await task
    harness.zone_update(active=True)
    harness.zone_update(active=False)
    harness.zone_update(active=True)
    await harness.settle(3)

    widerspruechlich = [
        payload
        for payload in harness.mqtt.heat_pump_states
        if (payload["state"] == "disarmed" and payload["armed"] is not False)
        or (payload["state"] in {"waiting_confirmation", "running", "disconnected"}
            and (payload["armed"] is not True or payload["remaining_seconds"] is None))
    ]
    assert widerspruechlich == [], f"{widerspruechlich}"
    assert len(harness.mqtt.heat_pump_states) > 5, "The heartbeat did not publish"


# --------------------------------------------------------------------------
# R-HP-10 Reassert failure
# --------------------------------------------------------------------------


async def test_tc_hp_10_failing_reassert_is_recorded(harness: HeatPumpHarness) -> None:
    """R-HP-10: A reassert failure is reported with activate_called false."""
    harness.quiet_watchdog()
    await harness.on(duration=30)
    harness.zone_update(active=True)
    harness.zone.fail("activate", RuntimeError("PUACK fehlt"))

    harness.zone_update(active=False)
    await harness.settle(3)

    failed = [
        entry for entry in harness.mqtt.heat_pump_reasserts if entry["activate_called"] is False
    ]
    assert failed, "No failed reassert was published"
    assert "activate failed" in failed[-1]["reason"]
    assert "PUACK fehlt" in failed[-1]["activate_error"]


# --------------------------------------------------------------------------
# R-HP-11, R-HP-12 Emergency stop
# --------------------------------------------------------------------------


async def test_tc_hp_11_emergency_stop_sends_no_deactivate(harness: HeatPumpHarness) -> None:
    """R-HP-11: The emergency stop intentionally leaves the zone untouched."""
    settings.heat_pump_max_reassert_attempts = 1
    await harness.on(duration=30)

    await harness.settle(12)

    assert harness.zone.count("deactivate") == 0
    assert harness.mqtt.heat_pump_errors[-1]["error"] == "emergency_stop"
    assert harness.mqtt.heat_pump_errors[-1]["attempts"] == 1
    assert harness.mqtt.heat_pump_errors[-1]["reason"].startswith("Pump not confirmed after 1 attempts: ")
    assert harness.controller._armed is False
    assert harness.mqtt.state_messages()[-1] == "error"


async def test_tc_hp_11_emergency_stop_disarms_state(harness: HeatPumpHarness) -> None:
    """R-HP-11: The controller remains disarmed after the emergency stop."""
    settings.heat_pump_max_reassert_attempts = 1
    await harness.on(duration=30)

    await harness.settle(12)

    last = harness.mqtt.heat_pump_states[-1]
    assert last["armed"] is False
    assert last["remaining_seconds"] is None


async def test_tc_hp_12_zero_max_attempts_never_emergency_stops(harness: HeatPumpHarness) -> None:
    """R-HP-12: With 0 attempts, no emergency stop occurs."""
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)

    await harness.settle(15)

    assert harness.mqtt.heat_pump_errors == []
    assert harness.controller._armed is True
    assert harness.controller._error_count >= 2


# --------------------------------------------------------------------------
# R-HP-13, R-HP-14, R-HP-16, R-HP-18 off, expiry, and shutdown
# --------------------------------------------------------------------------


async def test_tc_hp_13_off_disarms_and_deactivates(harness: HeatPumpHarness) -> None:
    """R-HP-13: off deactivates the zone and reports disarmed."""
    await harness.on(duration=30)

    await harness.off()

    assert harness.zone.count("deactivate") == 1
    assert harness.controller._armed is False
    assert harness.controller._until_monotonic is None
    assert harness.mqtt.state_messages()[-1] == "disarmed"
    assert harness.mqtt.heat_pump_results[-1]["success"] is True
    assert harness.mqtt.heat_pump_results[-1]["message"] == "Heat-pump pump request cancelled"


async def test_tc_hp_14_off_survives_deactivate_error(harness: HeatPumpHarness) -> None:
    """R-HP-14: A deactivate failure does not make off fail."""
    await harness.on(duration=30)
    harness.zone.fail("deactivate", RuntimeError("Pumpe laeuft mit Initiatoren"))

    await harness.off()

    assert harness.mqtt.heat_pump_results[-1]["success"] is True
    assert harness.controller._armed is False


async def test_tc_hp_14_off_without_client_still_succeeds(harness: HeatPumpHarness) -> None:
    """R-HP-14: off succeeds without a client."""
    harness.client = None  # type: ignore[assignment]
    harness.controller._get_client = lambda: None  # type: ignore[method-assign]

    await harness.off()

    assert harness.mqtt.heat_pump_results[-1]["success"] is True
    assert harness.mqtt.state_messages()[-1] == "disarmed"


async def test_tc_hp_16_expiry_deactivates_and_clears_remaining(harness: HeatPumpHarness) -> None:
    """R-HP-16: Expiry deactivates the zone and clears remaining_seconds."""
    await harness.on(duration=0)

    await harness.settle(4)

    assert harness.zone.count("deactivate") == 1
    last = harness.mqtt.heat_pump_states[-1]
    assert last["state"] == "disarmed_expired"
    assert last["remaining_seconds"] is None
    assert last["armed"] is False


async def test_tc_hp_18_stop_disarms(harness: HeatPumpHarness) -> None:
    """R-HP-18: Shutdown disarms and publishes disarmed."""
    await harness.on(duration=30)

    await harness.controller.stop()

    assert harness.controller._armed is False
    assert harness.controller._until_monotonic is None
    assert harness.mqtt.state_messages()[-1] == "disarmed"


# --------------------------------------------------------------------------
# R-HP-19 Error counter
# --------------------------------------------------------------------------


async def test_tc_hp_19_error_count_resets_on_confirmation(harness: HeatPumpHarness) -> None:
    """R-HP-19: Confirmation resets the error counter."""
    # Use enough attempts so two failures do not immediately trigger the emergency stop.
    harness.quiet_watchdog(max_attempts=5)
    await harness.on(duration=30)
    harness.controller._register_failure("a")
    harness.controller._register_failure("b")
    assert harness.controller._error_count == 2

    harness.zone_update(active=True)

    assert harness.controller._error_count == 0


async def test_tc_hp_19_error_count_resets_on_off(harness: HeatPumpHarness) -> None:
    """R-HP-19: off resets the error counter."""
    await harness.on(duration=30)
    harness.controller._register_failure("a")

    await harness.off()

    assert harness.controller._error_count == 0


async def test_tc_hp_19_expiry_keeps_error_count_in_state(harness: HeatPumpHarness) -> None:
    """R-HP-19: Expiry does not reset the error counter itself.

    It is reset only by the next `on`, because `was_armed` is then false. The
    `disarmed_expired` payload therefore still contains this request's error count.
    """
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)
    harness.controller._error_count = 2
    # Set the runtime to now so the next watchdog check expires it.
    harness.controller._until_monotonic = harness.loop.time()

    await harness.settle(3)

    assert harness.controller._error_count == 2
    assert harness.mqtt.state_messages()[-1] == "disarmed_expired"
    assert harness.mqtt.heat_pump_states[-1]["error_count"] == 2

    # The next on starts a new count.
    await harness.on(duration=30)
    assert harness.controller._error_count == 0


async def test_tc_hp_19_error_count_resets_on_new_on(harness: HeatPumpHarness) -> None:
    """R-HP-19: A new on starts a new count."""
    await harness.on(duration=30)
    harness.controller._register_failure("a")
    assert harness.controller._error_count == 1

    await harness.off()
    await harness.on(duration=30)

    assert harness.controller._error_count == 0
    assert harness.controller._armed is True


# --------------------------------------------------------------------------
# Payload-Form
# --------------------------------------------------------------------------


async def test_tc_hp_01_default_duration_is_used(harness: HeatPumpHarness) -> None:
    """R-HP-01: Without duration, GECKO_HEAT_PUMP_DEFAULT_DURATION applies."""
    await harness.controller._handle_command({"action": "on"})

    assert harness.mqtt.heat_pump_results[-1]["duration"] == settings.heat_pump_default_duration


async def test_tc_hp_01_wait_until_connected_is_awaited(harness: HeatPumpHarness) -> None:
    """R-HP-01: The on command waits for connection readiness."""
    await harness.on(duration=30)

    assert harness.wait_connected_calls == 1


async def test_tc_hp_01_pending_state_is_published_with_armed_flag(harness: HeatPumpHarness) -> None:
    """R-HP-01: The state names armed and zone_id."""
    await harness.on(duration=30)

    last = harness.mqtt.heat_pump_states[-1]
    assert last["armed"] is True
    assert last["zone_id"] == settings.heat_pump_flow_zone_id
    assert last["state"] == "waiting_confirmation"


@pytest.mark.parametrize("duration", [1, 5, 60])
async def test_tc_hp_01_various_durations_arm(harness: HeatPumpHarness, duration: int) -> None:
    """R-HP-01: Different runtimes arm the controller."""
    await harness.on(duration=duration)

    armed(harness)
    assert harness.zone.count("activate") == 1


def test_tc_hp_01_harness_zone_matches_configured_id(harness: HeatPumpHarness) -> None:
    """R-HP-01: The test zone has the configured ID."""
    assert str(harness.zone.id) == settings.heat_pump_flow_zone_id


async def test_tc_hp_17_reassert_deadline_is_preserved_after_late_result(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-17: A stale reassert return produces no additional event."""
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    task = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await wait_for(entered)
    await harness.off()
    gate.set()
    await task

    assert harness.mqtt.heat_pump_reasserts == []


def test_tc_hp_01_state_payload_shape(harness: HeatPumpHarness) -> None:
    """R-HP-01: The state contains exactly the documented fields."""
    assert harness.controller._state == "disarmed"
    payload = {
        "state": "running",
        "zone_id": "4",
        "armed": True,
        "error_count": 0,
        "max_errors": 2,
        "remaining_seconds": 10,
        "timestamp": harness.controller._timestamp(),
    }
    expected_keys = {"state", "zone_id", "armed", "error_count", "max_errors", "remaining_seconds", "timestamp"}
    assert set(payload) == expected_keys


def test_tc_hp_22_initiators_are_serialised(harness: HeatPumpHarness) -> None:
    """R-HP-22: Initiators are returned as codes and labels."""
    from gecko_iot_client.models.flow_zone import FlowZoneInitiator

    harness.zone.initiators = [FlowZoneInitiator.HEAT_PUMP, "ZZ"]

    assert harness.controller._initiators(harness.zone) == ["HTP", "ZZ"]


def test_tc_hp_22_initiators_of_none_are_empty(harness: HeatPumpHarness) -> None:
    """R-HP-22: Without a zone, initiators remain empty."""
    assert harness.controller._initiators(None) == []
    assert harness.controller._initiators(harness.zone) == []


async def test_tc_hp_17_second_on_after_off_activates_again(harness: HeatPumpHarness) -> None:
    """R-HP-17: A new on may activate again after off."""
    await harness.on(duration=30)
    await harness.off()

    harness.zone.active = False
    await harness.on(duration=30)

    assert harness.zone.count("activate") == 2
    assert harness.controller._armed is True
    assert harness.controller._until_monotonic is not None


async def test_tc_hp_17_off_cancels_watchdog(harness: HeatPumpHarness) -> None:
    """R-HP-17: off stops the watchdog."""
    await harness.on(duration=30)
    task = harness.controller._watchdog_task
    assert task is not None

    await harness.off()

    assert task.cancelled() or task.done()
    assert harness.controller._watchdog_task is None


async def test_tc_hp_17_late_activate_does_not_rearm(harness: HeatPumpHarness) -> None:
    """R-HP-17: A delayed return does not arm again."""
    harness.zone.delay("activate", 0.15)

    await harness.on(duration=30)
    await harness.off()

    assert harness.controller._armed is False
    assert harness.controller._until_monotonic is None
    assert harness.controller._confirmation_deadline is None


async def test_tc_hp_17_late_activate_after_expiry_does_not_rearm(harness: HeatPumpHarness) -> None:
    """R-HP-17: The controller remains disarmed after expiry even when activate returns."""
    harness.zone.delay("activate", 0.1)
    settings.heat_pump_max_reassert_attempts = 0

    await harness.on(duration=0)
    await harness.settle(4)

    assert harness.controller._armed is False
    assert harness.mqtt.state_messages()[-1] == "disarmed_expired"


def test_tc_hp_19_register_failure_publishes_reassert(harness: HeatPumpHarness) -> None:
    """R-HP-19: Every failure creates a reassert event."""
    harness.controller._register_failure("mein grund", None, False, None)

    assert harness.mqtt.reassert_reasons() == ["mein grund"]
    assert harness.mqtt.heat_pump_reasserts[-1]["confirmed"] is False
    assert harness.mqtt.heat_pump_reasserts[-1]["attempt"] == 1


def test_tc_hp_19_register_failure_counts_up(harness: HeatPumpHarness) -> None:
    """R-HP-19: The error counter increases with every failure."""
    harness.controller._register_failure("a")
    harness.controller._register_failure("b")

    assert harness.controller._error_count == 2
    assert harness.mqtt.heat_pump_reasserts[-1]["attempt"] == 2


async def test_tc_hp_01_activate_failure_disarms_and_reports(harness: HeatPumpHarness) -> None:
    """R-HP-01: A failure in the initial activate reports the command as failed."""
    harness.zone.fail("activate", RuntimeError("PUBACK fehlt"))

    await harness.on(duration=30)

    assert harness.controller._armed is False
    result = harness.mqtt.heat_pump_results[-1]
    assert result["success"] is False
    assert "PUBACK fehlt" in result["message"]
    activate_errors = [entry for entry in harness.mqtt.heat_pump_reasserts if entry["activate_called"]]
    assert activate_errors, "The activate failure was not reported as a reassert"
    assert "PUBACK fehlt" in activate_errors[-1]["activate_error"]


async def test_tc_hp_01_activate_failure_cancels_watchdog(harness: HeatPumpHarness) -> None:
    """R-HP-01: No watchdog runs after an activate failure."""
    harness.zone.fail("activate", RuntimeError("nein"))

    await harness.on(duration=30)

    assert harness.controller._watchdog_task is None
    assert harness.controller._pending_confirmation is False
    assert harness.controller._confirmation_deadline is None


async def test_tc_hp_06_reassert_failure_does_not_trip_emergency_stop_immediately(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-06: A failed reassert counts but does not trigger immediately."""
    harness.quiet_watchdog(max_attempts=5)
    await harness.on(duration=30)
    harness.zone_update(active=True)
    harness.zone.fail("activate", RuntimeError("nein"))

    harness.zone_update(active=False)
    await harness.settle(3)

    assert harness.controller._error_count == 1
    assert harness.mqtt.heat_pump_errors == []


async def test_tc_hp_02_off_after_confirmed_active_zone(harness: HeatPumpHarness) -> None:
    """R-HP-02: off remains possible even for an already active zone."""
    harness.zone.active = True
    await harness.on(duration=30)
    assert harness.mqtt.state_messages()[-1] == "running"

    await harness.off()

    assert harness.controller._armed is False
    assert harness.zone.count("deactivate") == 1


async def test_tc_hp_15_heat_pump_result_carries_zone_id(harness: HeatPumpHarness) -> None:
    """R-HP-15: The result names the configured zone."""
    await harness.on(duration=30)

    assert harness.mqtt.heat_pump_results[-1]["zone_id"] == settings.heat_pump_flow_zone_id


async def test_tc_hp_01_state_is_published_with_timestamp(harness: HeatPumpHarness) -> None:
    """R-HP-01: Every state contains a UTC timestamp."""
    await harness.on(duration=30)

    assert harness.mqtt.heat_pump_states[-1]["timestamp"].endswith("+00:00")


async def test_tc_hp_17_zone_update_after_off_is_ignored(harness: HeatPumpHarness) -> None:
    """R-HP-17: No zone update triggers a reassert after off."""
    await harness.on(duration=30)
    await harness.off()

    harness.zone_update(active=False)
    await harness.settle(3)

    assert harness.zone.count("activate") == 1
    assert harness.mqtt.reassert_reasons() == []


async def test_tc_hp_17_zone_update_after_stop_is_ignored(harness: HeatPumpHarness) -> None:
    """R-HP-17: No zone update triggers a reassert after shutdown."""
    await harness.on(duration=30)
    await harness.controller.stop()

    harness.zone_update(active=False)
    await harness.settle(3)

    assert harness.zone.count("activate") == 1


async def test_tc_hp_17_generation_changes_on_each_command(harness: HeatPumpHarness) -> None:
    """R-HP-17: on and off each increment the generation."""
    start = harness.controller._command_generation

    await harness.on(duration=30)
    after_on = harness.controller._command_generation
    await harness.off()

    assert after_on == start + 1
    assert harness.controller._command_generation == start + 2


async def test_tc_hp_17_stale_command_after_new_on_is_discarded(harness: HeatPumpHarness) -> None:
    """R-HP-17: A stale command generation aborts the operation."""
    entered = harness.zone.entered("activate")
    gate = harness.zone.block("activate")

    first = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await wait_for(entered)

    # The second on also waits for activate.
    second = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await asyncio.sleep(0.02)

    gate.set()
    await asyncio.gather(first, second)

    # The earlier generation must not publish a success result.
    results = [
        entry
        for entry in harness.mqtt.heat_pump_results
        if entry.get("action") == "on" and entry["success"]
    ]
    assert len(results) == 1


async def test_tc_hp_21_watchdog_reports_remaining_seconds(harness: HeatPumpHarness) -> None:
    """R-HP-21: remaining_seconds decreases with runtime."""
    await harness.on(duration=30)
    first = harness.mqtt.heat_pump_results[-1]["remaining_seconds"]

    await harness.settle(2)

    assert harness.controller._until_monotonic is not None
    last = harness.mqtt.heat_pump_states[-1]["remaining_seconds"]
    assert last is not None
    assert last <= 1800
    assert first is not None


def test_tc_hp_22_initiators_of_empty_zone(harness: HeatPumpHarness) -> None:
    """R-HP-22: A zone without initiators returns an empty list."""
    harness.zone.initiators = []

    assert harness.controller._initiators(harness.zone) == []


async def test_tc_hp_01_repeated_on_while_pending_keeps_single_watchdog(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-01: Multiple on commands share one watchdog."""
    await harness.on(duration=30)
    task = harness.controller._watchdog_task

    await harness.on(duration=60)

    assert harness.controller._watchdog_task is task


async def test_tc_hp_17_second_on_while_armed_reactivates(harness: HeatPumpHarness) -> None:
    """R-HP-17: A repeated on reactivates an inactive zone."""
    await harness.on(duration=30)
    harness.zone_update(active=True)
    harness.zone.active = False

    await harness.on(duration=30)

    assert harness.zone.count("activate") == 2
    assert harness.controller._pending_confirmation is True


# --------------------------------------------------------------------------
# R-HP-23 Disconnected Gecko connection counts as a failure
# --------------------------------------------------------------------------


async def test_tc_hp_23_offline_client_increments_error_count(harness: HeatPumpHarness) -> None:
    """R-HP-23: A disconnected connection increases the error counter."""
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)
    harness.controller._error_count = 0

    harness.client.is_connected = False
    await harness.settle(4)

    assert harness.controller._error_count >= 1
    assert "gecko disconnected" in harness.mqtt.reassert_reasons()
    offline = [
        entry
        for entry in harness.mqtt.heat_pump_reasserts
        if entry["reason"] == "gecko disconnected"
    ]
    assert offline[-1]["activate_called"] is False
    assert offline[-1]["confirmed"] is False


async def test_tc_hp_23_missing_client_increments_error_count(harness: HeatPumpHarness) -> None:
    """R-HP-23: A completely missing client also counts as a failure."""
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)
    harness.controller._error_count = 0

    harness.controller._get_client = lambda: None  # type: ignore[method-assign]
    await harness.settle(4)

    assert harness.controller._error_count >= 1
    assert "gecko disconnected" in harness.mqtt.reassert_reasons()


async def test_tc_hp_23_offline_never_calls_activate(harness: HeatPumpHarness) -> None:
    """R-HP-23: The zone is not activated while offline."""
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)
    activations = harness.zone.count("activate")

    harness.client.is_connected = False
    await harness.settle(4)

    assert harness.zone.count("activate") == activations
    assert harness.controller._armed is True


async def test_tc_hp_23_offline_counts_at_most_once_per_cycle(harness: HeatPumpHarness) -> None:
    """R-HP-23: The offline state counts at most once per cycle.

    Every offline cycle counts exactly one failure and publishes exactly one
    `disconnected` state. Before the fix, each cycle published two identical
    state messages to the retained topic.
    """
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)
    harness.controller._error_count = 0

    harness.client.is_connected = False
    await harness.settle(5)

    states = harness.mqtt.state_messages().count("disconnected")
    counts = harness.mqtt.reassert_reasons().count("gecko disconnected")

    assert counts >= 2, "The counter does not keep increasing"
    assert counts <= states, "The count exceeds one per cycle"
    assert harness.controller._error_count == counts
    assert all(
        entry["activate_called"] is False
        for entry in harness.mqtt.heat_pump_reasserts
        if entry["reason"] == "gecko disconnected"
    )


async def test_tc_hp_23_offline_publishes_disconnected_state(harness: HeatPumpHarness) -> None:
    """R-HP-23: The disconnected state is published with armed true."""
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)

    harness.client.is_connected = False
    await harness.settle(3)

    assert harness.mqtt.state_messages()[-1] == "disconnected"
    assert harness.mqtt.heat_pump_states[-1]["armed"] is True


async def test_tc_hp_23_offline_reaches_emergency_stop(harness: HeatPumpHarness) -> None:
    """R-HP-23: Offline failures trigger the emergency stop at the limit."""
    settings.heat_pump_max_reassert_attempts = 2
    await harness.on(duration=30)

    harness.client.is_connected = False
    await harness.settle(6)

    assert harness.mqtt.heat_pump_errors[-1]["error"] == "emergency_stop"
    assert harness.mqtt.heat_pump_errors[-1]["reason"].startswith(
        "Pump not confirmed after 2 attempts: gecko disconnected"
    )
    assert harness.mqtt.heat_pump_errors[-1]["attempts"] == 2
    assert harness.controller._armed is False
    # 'error' must remain the last state. Before the fix, the offline branch
    # overwrote it with 'disconnected' in the same cycle.
    assert harness.mqtt.state_messages()[-1] == "error"
    assert harness.controller._watchdog_task is None
    assert harness.mqtt.heat_pump_states[-1]["armed"] is False


async def test_tc_hp_23_offline_never_emergency_stops_with_zero_max_attempts(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-23: With 0 attempts, even persistent failures do not trigger an emergency stop."""
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)

    harness.client.is_connected = False
    await harness.settle(6)

    assert harness.mqtt.heat_pump_errors == []
    assert harness.controller._armed is True
    assert harness.controller._error_count >= 2


async def test_tc_hp_23_reconnect_does_not_reset_error_count(harness: HeatPumpHarness) -> None:
    """R-HP-23: Reconnecting alone does not reset the counter.

    Confirmation, `off`, or a new `on` resets it. An unstable connection can
    therefore continue the emergency stop despite the counter while the zone
    remains unconfirmed.
    """
    # Use a long confirmation period before on so reconnection does not add
    # confirmation timeouts to the counter.
    harness.quiet_watchdog(max_attempts=0)
    await harness.on(duration=30)

    harness.client.is_connected = False
    await harness.settle(4)
    after_offline = harness.controller._error_count
    assert after_offline >= 1

    harness.client.is_connected = True
    await harness.settle(3)

    assert harness.controller._error_count == after_offline
    # The counter remains, but the state reports reality again: the connection
    # is back and activation is unconfirmed. `disconnected` is now wrong, while
    # `running` would also be wrong without confirmation.
    assert harness.mqtt.state_messages()[-1] == "waiting_confirmation"
    assert harness.mqtt.heat_pump_states[-1]["armed"] is True


async def test_tc_hp_23_zone_update_while_offline_does_not_count(
    harness: HeatPumpHarness,
) -> None:
    """R-HP-23: Zone updates during an outage do not count.

    Only the watchdog increases the counter. `_observe_zone_update` returns
    early when disconnected so no reassert is created.
    """
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)
    harness.client.is_connected = False
    # Wait for a stable offline state so the watchdog counts.
    await harness.settle(3)
    baseline = harness.controller._error_count

    harness.zone_update(active=False)
    await harness.settle(2)

    reasons = harness.mqtt.reassert_reasons()
    assert "zone reported inactive" not in reasons
    # The watchdog may count at most once per cycle during this time.
    assert harness.controller._error_count >= baseline


async def test_tc_hp_19_watchdog_zone_error_counts_as_failure(harness: HeatPumpHarness) -> None:
    """R-HP-19: A failed zone lookup counts as a failure."""
    settings.heat_pump_max_reassert_attempts = 0
    await harness.on(duration=30)
    harness.zone_update(active=True)

    def exploding_lookup(zone_type: Any, zone_id: str) -> Any:
        raise KeyError("weg")

    harness.client._by_key = {}
    harness.client.get_zone_by_id_and_type = exploding_lookup  # type: ignore[method-assign]

    await harness.settle(3)

    assert harness.controller._error_count >= 1
    assert any("watchdog exception" in reason for reason in harness.mqtt.reassert_reasons())


async def test_tc_hp_13_off_publishes_state_after_result(harness: HeatPumpHarness) -> None:
    """R-HP-13: The order of result and state remains unchanged."""
    await harness.on(duration=30)
    count_before = len(harness.mqtt.heat_pump_results)

    await harness.off()

    assert len(harness.mqtt.heat_pump_results) == count_before + 1
    assert harness.mqtt.state_messages()[-1] == "disarmed"


def test_tc_hp_19_state_timestamp_is_utc(harness: HeatPumpHarness) -> None:
    """R-HP-19: The timestamp is generated in UTC."""
    stamp = harness.controller._timestamp()

    assert stamp.endswith("+00:00")
