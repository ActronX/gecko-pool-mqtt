"""Requirements R-THR: concurrency and thread boundaries.

TC-THR-01 is deliberately static: it uses the AST to verify that no blocking
Gecko mutation sits outside ``asyncio.to_thread``. This rule is the bridge's
most important non-functional requirement and can be enforced continuously
without Gecko or a broker.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from gecko_iot_client import ZoneType

from app.config import settings

from .conftest import HeatPumpHarness, PoolHarness
from .fakes import FakeZone
from .test_heat_pump_controller import wait_for

APP_DIR = Path(__file__).resolve().parent.parent / "app"

#: Blocking zone methods in gecko-iot-client 1.0.3 (PUBACK wait).
BLOCKING_MUTATIONS = {
    "activate",
    "deactivate",
    "set_speed",
    "set_color",
    "set_effect",
    "set_target_temperature",
}

#: Pure helpers called exclusively through asyncio.to_thread.
ALLOWED_HELPERS = {"set_target_temperature", "set_light", "set_flow"}

#: Local read accesses that per the requirement stay on the event loop.
LOCAL_READS = {"is_connected", "active", "initiators", "get_zone_by_id_and_type"}


def app_sources() -> list[Path]:
    return sorted(APP_DIR.glob("*.py"))


class MutationVisitor(ast.NodeVisitor):
    """Collects blocking calls and associates them with the enclosing function."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.allowed_nodes: set[int] = set()
        self.violations: list[tuple[str, int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "to_thread":
                # Arguments to to_thread are offloaded by definition.
                for argument in list(node.args) + [kw.value for kw in node.keywords]:
                    self.allowed_nodes.add(id(argument))
            elif node.func.attr in BLOCKING_MUTATIONS:
                inside_helper = bool(self.stack) and self.stack[-1] in ALLOWED_HELPERS
                if id(node) not in self.allowed_nodes and not inside_helper:
                    owner = self.stack[-1] if self.stack else "<module>"
                    self.violations.append((owner, node.lineno, node.func.attr))
        self.generic_visit(node)


def collect_violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for path in app_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = MutationVisitor()
        visitor.visit(tree)
        for owner, lineno, method in visitor.violations:
            violations.append((f"{path.name}:{lineno} ({owner})", lineno, method))
    return violations


def offloaded_reads() -> list[str]:
    """Finds local read accesses that were offloaded by mistake."""
    findings: list[str] = []
    for path in app_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "to_thread":
                continue
            for argument in list(node.args) + [kw.value for kw in node.keywords]:
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Attribute) and inner.attr in LOCAL_READS:
                        findings.append(f"{path.name}:{inner.lineno} {inner.attr}")
    return findings


# --------------------------------------------------------------------------
# TC-THR-01 statische Absicherung
# --------------------------------------------------------------------------


def test_tc_thr_01_no_blocking_mutation_outside_to_thread() -> None:
    """R-THR-01: Blocking Gecko mutations run only inside asyncio.to_thread."""
    violations = collect_violations()

    assert violations == [], (
        "Blocking Gecko calls outside asyncio.to_thread found: "
        f"{violations}"
    )


def test_tc_thr_01_all_mutation_methods_are_covered() -> None:
    """R-THR-01: The test captures exactly the six known methods."""
    found: set[str] = set()
    for path in app_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in BLOCKING_MUTATIONS:
                    found.add(node.func.attr)

    assert found == BLOCKING_MUTATIONS, (
        f"Expected all six blocking methods, found: {sorted(found)}"
    )


def test_tc_thr_01_local_reads_stay_on_the_event_loop() -> None:
    """R-THR-01: Pure reads are not offloaded."""
    assert offloaded_reads() == []


def test_tc_thr_01_to_thread_is_the_only_offload_mechanism() -> None:
    """R-THR-01: run_in_executor is not used additionally."""
    for path in app_sources():
        source = path.read_text(encoding="utf-8")
        assert "run_in_executor" not in source, f"{path.name} nutzt run_in_executor"


# --------------------------------------------------------------------------
# TC-THR-02 The event loop remains responsive
# --------------------------------------------------------------------------


async def test_tc_thr_02_blocking_call_does_not_stop_event_loop(harness: HeatPumpHarness) -> None:
    """R-THR-04: A blocking Gecko call does not stop the event loop."""
    settings.heat_pump_max_reassert_attempts = 0
    harness.zone.delay("activate", 0.3)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    heartbeat = asyncio.create_task(ticker())
    states_before = len(harness.mqtt.heat_pump_states)
    states_at_start = states_before

    task = asyncio.create_task(harness.controller._handle_command({"action": "on", "duration": 30}))
    await asyncio.sleep(0.15)
    states_mid = len(harness.mqtt.heat_pump_states)
    ticks_mid = ticks

    await task
    await asyncio.sleep(0.1)
    heartbeat.cancel()

    assert ticks_mid > 5, f"The event loop stalled: only {ticks_mid} heartbeats"
    assert states_mid > states_at_start, "The watchdog published no state"
    assert len(harness.mqtt.heat_pump_states) > states_mid


# --------------------------------------------------------------------------
# TC-THR-07 Saturation of the default executor
# --------------------------------------------------------------------------


def default_executor_workers() -> int:
    """Size of the default executor as CPython creates it."""
    return min(32, (os.cpu_count() or 1) + 4)


async def test_tc_thr_07_burst_of_blocking_commands_stays_responsive(pool) -> None:
    """R-THR-04: A burst of blocking commands does not block the event loop.

    This reproduces the realistic case where several zones are switched at
    once. Every call blocks in a worker thread.
    """
    block_seconds = 0.3
    workers = default_executor_workers()
    count = min(4, workers)

    zones = [
        pool.add_zone(FakeZone(str(index)), ZoneType.FLOW_ZONE)
        for index in range(1, count + 1)
    ]
    for zone in zones:
        zone.delay("activate", block_seconds)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    heartbeat = asyncio.create_task(ticker())
    started = asyncio.get_running_loop().time()

    await asyncio.gather(
        *(
            pool.controller._handle_command("flow", zone.id, {"action": "on"})
            for zone in zones
        )
    )

    elapsed = asyncio.get_running_loop().time() - started
    heartbeat.cancel()

    assert all(zone.count("activate") == 1 for zone in zones)
    assert all(entry["success"] for entry in pool.mqtt.results), pool.mqtt.results
    # The loop would not have managed a single heartbeat with blocking
    # execution on the main thread.
    assert ticks > 5, f"The event loop stalled: only {ticks} heartbeats"


async def test_tc_thr_07_burst_runs_in_parallel(pool) -> None:
    """R-THR-04: Blocking calls run concurrently, not one after another.

    Without `to_thread` the four calls would have run one after another and
    the total time would equal the sum. With offloading it is roughly as large
    as a single call. The limit is the default executor; more concurrent calls
    than it has threads are queued.
    """
    block_seconds = 0.3
    workers = default_executor_workers()
    count = min(4, workers)

    zones = [
        pool.add_zone(FakeZone(str(index)), ZoneType.FLOW_ZONE)
        for index in range(1, count + 1)
    ]
    for zone in zones:
        zone.delay("activate", block_seconds)

    started = asyncio.get_running_loop().time()
    await asyncio.gather(
        *(
            pool.controller._handle_command("flow", zone.id, {"action": "on"})
            for zone in zones
        )
    )
    elapsed = asyncio.get_running_loop().time() - started

    sequential = block_seconds * count
    assert elapsed < sequential * 0.75, (
        f"{count} calls at {block_seconds}s needed {elapsed:.2f}s, "
        f"sequentially it would be {sequential:.2f}s"
    )


async def test_tc_thr_07_queue_depth_adds_latency(pool) -> None:
    """R-THR-04: More concurrent calls than executor threads get queued.

    Documents the upper bound: the default executor has
    `min(32, cpu_count + 4)` threads. Every further blocking call waits, which
    adds up to 5 seconds of waiting per queue slot. The event loop stays
    responsive throughout.
    """
    block_seconds = 0.2
    workers = default_executor_workers()
    # Well above the capacity, so queueing is unavoidable.
    count = workers + 2

    zones = [
        pool.add_zone(FakeZone(str(index)), ZoneType.FLOW_ZONE)
        for index in range(1, count + 1)
    ]
    for zone in zones:
        zone.delay("activate", block_seconds)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    heartbeat = asyncio.create_task(ticker())
    started = asyncio.get_running_loop().time()

    await asyncio.gather(
        *(
            pool.controller._handle_command("flow", zone.id, {"action": "on"})
            for zone in zones
        )
    )

    elapsed = asyncio.get_running_loop().time() - started
    heartbeat.cancel()

    assert all(zone.count("activate") == 1 for zone in zones)
    # Without queueing the time would be about capacity times 0.2s. Queueing
    # must add at least one further slot.
    waves = -(-count // workers)
    assert elapsed >= block_seconds * waves * 0.8, (
        f"expected at least {block_seconds * waves:.2f}s, measured {elapsed:.2f}s"
    )
    # The event loop stayed responsive despite the queue.
    assert ticks > 5, f"The event loop stalled: only {ticks} heartbeats"


# --------------------------------------------------------------------------
# TC-THR-03, TC-THR-04 No unhandled task exceptions
# --------------------------------------------------------------------------


async def test_tc_thr_03_cancelled_task_raises_no_unhandled_exception(harness: HeatPumpHarness) -> None:
    """R-THR-05: A cancelled task produces no unhandled exception."""
    loop = asyncio.get_running_loop()
    captured: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    try:
        entered = harness.zone.entered("activate")
        gate = harness.zone.block("activate")
        task = asyncio.create_task(
            harness.controller._handle_command({"action": "on", "duration": 30})
        )
        await wait_for(entered)

        task.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(previous)

    assert captured == [], f"Unbehandelte Task-Exception: {captured}"


async def test_tc_thr_04_reassert_task_swallows_exceptions(harness: HeatPumpHarness) -> None:
    """R-THR-06: The reassert task handles its exceptions internally."""
    loop = asyncio.get_running_loop()
    captured: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    try:
        await harness.on(duration=30)
        harness.zone_update(active=True)
        harness.zone.fail("activate", RuntimeError("Reassert scheitert"))

        harness.zone_update(active=False)
        await harness.settle(3)
    finally:
        loop.set_exception_handler(previous)

    assert captured == [], f"Unbehandelte Task-Exception: {captured}"
    assert harness.controller._error_count >= 1


# --------------------------------------------------------------------------
# TC-THR-05, TC-THR-06 Marshalling
# --------------------------------------------------------------------------


async def test_tc_thr_05_mqtt_command_is_marshalled_to_the_loop(harness: HeatPumpHarness) -> None:
    """R-THR-02: MQTT commands are routed onto the event loop."""
    harness.quiet_watchdog()

    harness.controller.handle_command({"action": "on", "duration": 30})
    await harness.settle(3)

    assert harness.zone.count("activate") == 1
    assert harness.controller._armed is True
    assert any(
        entry.get("action") == "on" and entry["success"] for entry in harness.mqtt.heat_pump_results
    )


async def test_tc_thr_06_zone_update_is_marshalled_to_the_loop(harness: HeatPumpHarness) -> None:
    """R-THR-03: Gecko zone updates are routed onto the event loop."""
    await harness.on(duration=30)
    harness.zone.active = True

    harness.controller.observe_zone_update({ZoneType.FLOW_ZONE: [harness.zone]})
    await asyncio.sleep(0.02)

    assert harness.controller._pending_confirmation is False
    assert harness.mqtt.state_messages()[-1] == "running"


async def test_tc_thr_06_marshalled_update_uses_current_zone_instance(
    harness: HeatPumpHarness,
) -> None:
    """R-THR-03: Callbacks receive the same zone instance as the controller."""
    await harness.on(duration=30)
    assert harness.zone.active is False

    harness.zone.active = True
    harness.controller.observe_zone_update({ZoneType.FLOW_ZONE: [harness.zone]})
    await asyncio.sleep(0.02)

    assert harness.controller._pending_confirmation is False
    assert harness.controller._error_count == 0
