from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from gecko_iot_client import GeckoIotClient, ZoneType
from gecko_iot_client.models.flow_zone import FlowZoneInitiator

from .config import settings
from .mqtt_bridge import MqttBridge

logger = logging.getLogger(__name__)

class HeatPumpController:
    """Maintain a timed flow-zone request for an external heat pump."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        mqtt: MqttBridge,
        get_client: Callable[[], GeckoIotClient | None],
        is_authenticated: Callable[[], bool],
        wait_until_connected: Callable[[], Awaitable[None]],
    ) -> None:
        self.loop = loop
        self.mqtt = mqtt
        self._get_client = get_client
        self._is_authenticated = is_authenticated
        self._wait_until_connected = wait_until_connected
        self._armed = False
        self._until_monotonic: float | None = None
        self._error_count = 0
        self._last_offline_count_at = float("-inf")
        self._pending_confirmation = False
        self._confirmation_deadline: float | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._command_generation = 0
        self._state = "disarmed"
        try:
            loop.call_soon_threadsafe(self._publish_state, "disarmed")
        except RuntimeError:
            logger.debug("Unable to publish initial heat-pump state; event loop is closed")

    def handle_command(self, data: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(self._handle_command(data), self.loop)

    def observe_zone_update(self, zones_dict: dict) -> None:
        self.loop.call_soon_threadsafe(self._observe_zone_update, zones_dict)

    async def _handle_command(self, data: dict[str, Any]) -> None:
        action = data.get("action")
        zone_id = settings.heat_pump_flow_zone_id
        if action == "off":
            self._command_generation += 1
            self._armed = False
            self._until_monotonic = None
            self._error_count = 0
            self._pending_confirmation = False
            self._confirmation_deadline = None
            self._cancel_watchdog()
            try:
                client = self._get_client()
                if client:
                    zone = client.get_zone_by_id_and_type(ZoneType.FLOW_ZONE, zone_id)
                    await asyncio.to_thread(zone.deactivate)
            except Exception as exc:
                logger.info("Unable to deactivate heat-pump zone: %s", exc)
            self.mqtt.publish_heat_pump_result(True, "Heat-pump pump request cancelled", action="off", zone_id=zone_id)
            self._publish_state("disarmed")
            return

        if action != "on":
            self.mqtt.publish_heat_pump_result(False, f"Unknown heat-pump action: {action}", action=action, zone_id=zone_id)
            return

        try:
            command_generation = self._command_generation = self._command_generation + 1
            client = self._get_client()
            if not self._is_authenticated() or not client:
                raise RuntimeError("Gecko client is not authenticated")
            await self._wait_until_connected()
            if command_generation != self._command_generation:
                return
            zone = client.get_zone_by_id_and_type(ZoneType.FLOW_ZONE, zone_id)
            now = self.loop.time()
            duration = int(data.get("duration", settings.heat_pump_default_duration))
            new_until = now + duration * 60
            was_armed = self._armed
            needs_activation = zone.active is not True
            self._until_monotonic = max(self._until_monotonic or 0, new_until)
            if not was_armed or self._watchdog_task is None or self._watchdog_task.done():
                self._armed = True
                self._error_count = 0
                self._pending_confirmation = needs_activation
                self._confirmation_deadline = now + settings.heat_pump_confirm_timeout if needs_activation else None
                self._watchdog_task = self.loop.create_task(self._watchdog())
            elif needs_activation:
                self._pending_confirmation = True
                self._confirmation_deadline = now + settings.heat_pump_confirm_timeout
            if needs_activation:
                try:
                    await asyncio.to_thread(zone.activate)
                except Exception as exc:
                    if command_generation == self._command_generation:
                        self._armed = False
                        self._until_monotonic = None
                        self._pending_confirmation = False
                        self._confirmation_deadline = None
                        self._cancel_watchdog()
                        self._register_failure(f"activate failed: {exc}", zone, True, str(exc))
                    raise
                if command_generation != self._command_generation or not self._armed:
                    return
                if needs_activation and zone.active is True:
                    # Confirmation may already have arrived in the Gecko thread
                    # while the blocking call was running. The zone is then
                    # running, and `waiting_confirmation` would be
                    # an incorrect report.
                    self._pending_confirmation = False
                    self._confirmation_deadline = None
                    self._error_count = 0
            remaining = max(0, int((self._until_monotonic or now) - now))
            # The result reports the state produced by this command. Reading
            # `self._state` would return the state from *before* the command,
            # such as `disarmed` after a restart.
            new_state = "waiting_confirmation" if self._pending_confirmation else "running"
            self.mqtt.publish_heat_pump_result(True, "Heat-pump pump request active", action="on", zone_id=zone_id, duration=duration, remaining_seconds=remaining, state=new_state)
            self._publish_state(new_state)
        except Exception as exc:
            logger.error("Heat-pump command failed: %s", exc)
            self.mqtt.publish_heat_pump_result(False, str(exc), action=action, zone_id=zone_id)

    async def _watchdog(self) -> None:
        try:
            while self._armed and self._until_monotonic is not None:
                now = self.loop.time()
                if now >= self._until_monotonic:
                    self._armed = False
                    self._until_monotonic = None
                    self._pending_confirmation = False
                    self._confirmation_deadline = None
                    self._cancel_watchdog()
                    await self._deactivate_best_effort()
                    self._publish_state("disarmed_expired")
                    return
                client = self._get_client()
                # True if this cycle has already published the state itself.
                # Otherwise it is published exactly once at the cycle end,
                # rather than twice with an identical payload in the same cycle.
                state_published = False
                if not client or not client.is_connected:
                    if now - self._last_offline_count_at >= settings.heat_pump_check_interval - 1e-6:
                        self._last_offline_count_at = now
                        # The return value indicates that the emergency stop was
                        # triggered. `disconnected` must not overwrite `error`
                        # then, or the retained payload would permanently show
                        # disconnected with armed false.
                        if self._register_failure("gecko disconnected"):
                            return
                    self._state = "disconnected"
                else:
                    try:
                        zone = client.get_zone_by_id_and_type(ZoneType.FLOW_ZONE, settings.heat_pump_flow_zone_id)
                        if self._pending_confirmation:
                            if zone.active is True:
                                self._pending_confirmation = False
                                self._confirmation_deadline = None
                                self._error_count = 0
                                self._state = "running"
                            elif self._confirmation_deadline is not None and now >= self._confirmation_deadline:
                                if self._register_failure("activation not confirmed within confirm_timeout"):
                                    # The emergency stop has already published
                                    # `error` and ended the watchdog.
                                    return
                                state_published = await self._reassert(zone, "activation confirmation timeout")
                            else:
                                # `activate()` is still blocking, and the command
                                # has not set the state name yet. The state is by
                                # definition `waiting_confirmation`; otherwise
                                # cycle completion would publish the constructor's
                                # previous value and report `disarmed` with
                                # `armed: true`.
                                self._state = "waiting_confirmation"
                        elif zone.active is True:
                            # Heal `disconnected` after reconnecting.
                            self._state = "running"
                        else:
                            state_published = await self._reassert(zone, "zone became inactive")
                    except Exception as exc:
                        logger.info("Heat-pump zone unavailable during watchdog check: %s", exc)
                        self._register_failure(f"watchdog exception: {exc}")
                if not state_published:
                    self._publish_state(self._state)
                await asyncio.sleep(settings.heat_pump_check_interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Heat-pump watchdog failed")
            self._register_failure(f"watchdog exception: {exc}")
        finally:
            if self._watchdog_task is asyncio.current_task():
                self._watchdog_task = None

    def _observe_zone_update(self, zones_dict: dict) -> None:
        zones = zones_dict.get(ZoneType.FLOW_ZONE, [])
        zone = next((item for item in zones if str(item.id) == settings.heat_pump_flow_zone_id), None)
        if zone is None or not self._armed:
            return
        client = self._get_client()
        if not client or not client.is_connected:
            return
        if zone.active is True:
            # Publish only the confirming transition or a name change. Gecko
            # sends zone updates for all zones and often several in quick
            # succession; without this condition every app button press would
            # produce another `running` publish.
            bestaetigt = self._pending_confirmation
            self._pending_confirmation = False
            self._confirmation_deadline = None
            self._error_count = 0
            if bestaetigt or self._state != "running":
                self._publish_state("running")
        elif zone.active is False and not self._pending_confirmation:
            # Pending must be set synchronously. The task starts only in the
            # next loop step; without this line the watchdog could start a
            # second reassert in the same window because it also sees the zone
            # as inactive.
            self._pending_confirmation = True
            self.loop.create_task(self._reassert(zone, "zone reported inactive"))

    async def _reassert(self, zone: Any, reason: str) -> bool:
        """Set the zone active again and report the reassert.

        Return value: True if the retained state was published during it.
        """
        deadline = self.loop.time() + settings.heat_pump_confirm_timeout
        self._pending_confirmation = True
        self._confirmation_deadline = deadline
        try:
            await asyncio.to_thread(zone.activate)
            if not self._armed or self._confirmation_deadline != deadline:
                return False
            if zone.active is True:
                # Gecko confirmed during the blocking call. The zone-update
                # callback has not run yet, or it would have cleared the
                # deadline. Without this check, the controller would publish
                # `waiting_confirmation` for a zone that is already running.
                # Otherwise the state report would be misleading.
                self._pending_confirmation = False
                self._confirmation_deadline = None
                self._error_count = 0
                self._publish_reassert(zone, reason, True, None, confirmed=True)
                self._publish_state("running")
                return True
            self._publish_reassert(zone, reason, True, None)
            self._publish_state("waiting_confirmation")
            return True
        except Exception as exc:
            if not self._armed or self._confirmation_deadline != deadline:
                return False
            logger.exception("Failed to reassert heat-pump flow zone")
            self._register_failure(f"activate failed: {exc}", zone, False, str(exc))
            return False

    def _register_failure(self, reason: str, zone: Any = None, activate_called: bool = False, activate_error: str | None = None) -> bool:
        self._error_count += 1
        self._publish_reassert(zone, reason, activate_called, activate_error)
        max_attempts = settings.heat_pump_max_reassert_attempts
        if max_attempts > 0 and self._error_count >= max_attempts:
            initiators = self._initiators(zone)
            self.mqtt.publish_heat_pump_error({"timestamp": self._timestamp(), "zone_id": settings.heat_pump_flow_zone_id, "error": "emergency_stop", "reason": f"Pump not confirmed after {self._error_count} attempts: {reason}", "attempts": self._error_count, "initiators": initiators})
            self._armed = False
            self._until_monotonic = None
            self._pending_confirmation = False
            self._confirmation_deadline = None
            self._cancel_watchdog()
            self._publish_state("error")
            return True
        return False

    def _publish_reassert(self, zone: Any, reason: str, activate_called: bool, activate_error: str | None, confirmed: bool = False) -> None:
        self.mqtt.publish_heat_pump_reassert({"timestamp": self._timestamp(), "zone_id": settings.heat_pump_flow_zone_id, "reason": reason, "initiators": self._initiators(zone), "attempt": self._error_count, "activate_called": activate_called, "activate_error": activate_error, "confirmed": confirmed})

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _initiators(zone: Any) -> list[str]:
        if not zone:
            return []
        return [item.value if isinstance(item, FlowZoneInitiator) else str(getattr(item, "value", item)) for item in (getattr(zone, "initiators", None) or [])]

    def _publish_state(self, state: str) -> None:
        self._state = state
        remaining = None
        if self._until_monotonic is not None:
            remaining = max(0, int(self._until_monotonic - self.loop.time()))
        self.mqtt.publish_heat_pump_state({"state": state, "zone_id": settings.heat_pump_flow_zone_id, "armed": self._armed, "error_count": self._error_count, "max_errors": settings.heat_pump_max_reassert_attempts, "remaining_seconds": remaining, "timestamp": self._timestamp()})

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done() and self._watchdog_task is not asyncio.current_task():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _deactivate_best_effort(self) -> None:
        try:
            client = self._get_client()
            if client:
                zone = client.get_zone_by_id_and_type(ZoneType.FLOW_ZONE, settings.heat_pump_flow_zone_id)
                await asyncio.to_thread(zone.deactivate)
        except Exception as exc:
            logger.info("Heat-pump runtime expired; Gecko retained control: %s", exc)

    async def stop(self) -> None:
        self._command_generation += 1
        self._armed = False
        self._until_monotonic = None
        self._error_count = 0
        self._pending_confirmation = False
        self._confirmation_deadline = None
        self._cancel_watchdog()
        self._publish_state("disarmed")
