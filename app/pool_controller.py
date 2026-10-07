from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
from gecko_iot_client import EventChannel, GeckoIotClient, ZoneType
from gecko_iot_client.transporters.mqtt import MqttTransporter

from .config import settings
from .gecko_api import SidecarGeckoApi
from .heat_pump_controller import HeatPumpController
from .mqtt_bridge import MqttBridge
from .oauth_flow import OAuthAuthenticationError, OAuthFlow
from .zone_serializer import serialize_connectivity, serialize_zones_by_type

logger = logging.getLogger(__name__)


class PoolController:
    def __init__(self, loop: asyncio.AbstractEventLoop, mqtt_bridge: MqttBridge):
        self.loop = loop
        self.mqtt = mqtt_bridge
        self.oauth_flow: OAuthFlow | None = None
        self.api: SidecarGeckoApi | None = None
        self.client: GeckoIotClient | None = None
        self.websession: aiohttp.ClientSession | None = None
        self.authenticated = False
        self.reauth_required = False
        self.auth_reason = ""
        self.vessels: list[dict[str, Any]] = []
        self.account_id = ""
        self.account_name = ""
        self.selected_monitor_id = ""
        self._connect_thread: threading.Thread | None = None
        self._connect_generation = 0
        self._recovery_task: asyncio.Task[None] | None = None
        self._stop_event = threading.Event()
        self.heat_pump = HeatPumpController(
            loop,
            mqtt_bridge,
            lambda: self.client,
            lambda: self.authenticated,
            self._wait_until_connected,
        )

    def _ensure_websession(self) -> aiohttp.ClientSession:
        if self.websession is None or self.websession.closed:
            self.websession = aiohttp.ClientSession()
        return self.websession

    async def try_start_from_tokens(self) -> bool:
        self.oauth_flow = OAuthFlow(self._ensure_websession())
        if not self.oauth_flow.has_tokens():
            await self.publish_login_challenge("no persisted OAuth tokens")
            return False
        try:
            await self.start_gecko_client()
            return True
        except OAuthAuthenticationError as exc:
            await self._mark_reauth_required(exc)
            return False
        except Exception as exc:
            logger.error("Auto-start from tokens failed: %s", exc)
            await self.publish_login_challenge(str(exc))
            return False

    async def start_gecko_client(self) -> bool:
        assert self.oauth_flow is not None
        current_task = asyncio.current_task()
        if self._recovery_task is not None and self._recovery_task is not current_task:
            self._recovery_task.cancel()
            self._recovery_task = None
        self._connect_generation += 1
        generation = self._connect_generation
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("Error disconnecting previous Gecko client")
            self.client = None
        self.api = SidecarGeckoApi(self._ensure_websession(), self.oauth_flow)
        if settings.account_id:
            self.account_id = settings.account_id
        else:
            self.account_id, account = await self.api.async_discover_account()
            self.account_name = account.get("name", "")
        self.vessels = await self.api.async_get_vessels(self.account_id)
        if not self.vessels:
            raise RuntimeError(f"No vessels found for account {self.account_id}")
        if settings.monitor_id:
            self.selected_monitor_id = settings.monitor_id
        else:
            vessel = self.vessels[0]
            self.selected_monitor_id = str(vessel.get("monitorId") or vessel.get("vesselId", ""))
        if not self.selected_monitor_id:
            raise RuntimeError("No monitorId found in first vessel")
        broker_url = await self.api.async_get_broker_url(self.selected_monitor_id)
        refresh_cb = self.api.make_sync_refresh_callback(self.loop, self._mark_reauth_required)
        transporter = MqttTransporter(
            broker_url=broker_url,
            monitor_id=self.selected_monitor_id,
            token_refresh_callback=refresh_cb,
        )
        self.client = GeckoIotClient(
            idd=self.selected_monitor_id,
            transporter=transporter,
            config_timeout=settings.config_timeout,
        )
        self._register_callbacks(self.client, generation)
        self.authenticated = False
        self.reauth_required = False
        self.auth_reason = ""
        self.mqtt.publish_auth_status({"status": "authenticating", "reason": "connecting to Gecko"})
        self._connect_thread = threading.Thread(
            target=self._connect_worker,
            args=(self.client, generation),
            daemon=True,
        )
        self._connect_thread.start()
        self.mqtt.publish_connectivity(self.get_connectivity_serialized())
        self.loop.call_soon(self._publish_snapshot_safe)
        return True

    def _connect_worker(self, client: GeckoIotClient, generation: int) -> None:
        delay = 5.0
        while not self._stop_event.is_set() and generation == self._connect_generation:
            try:
                client.connect()
                logger.info("GeckoIotClient connected successfully")
                if generation == self._connect_generation and self.client is client:
                    self.loop.call_soon_threadsafe(self._mark_connected, generation)
                return
            except Exception as exc:
                logger.error("GeckoIotClient connect failed: %s; retrying in %.0fs", exc, delay)
                try:
                    client.disconnect()
                except Exception:
                    logger.debug("Error cleaning up failed Gecko connection", exc_info=True)
                if self._stop_event.wait(delay):
                    return
                delay = min(delay * 2, 300.0)

    def _mark_connected(self, generation: int) -> None:
        if generation != self._connect_generation or self.client is None:
            return
        self.authenticated = True
        self.reauth_required = False
        self.auth_reason = ""
        self.mqtt.publish_auth_status({"status": "authenticated", "reason": ""})
        self._publish_snapshot_safe()

    async def complete_oauth(self, code: str, state: str | None = None) -> None:
        assert self.oauth_flow is not None
        try:
            await self.oauth_flow.exchange_code(code, state)
            await self.start_gecko_client()
            self.mqtt.clear_challenge()
        except OAuthAuthenticationError as exc:
            await self._mark_reauth_required(exc)
            raise

    async def publish_login_challenge(self, reason: str) -> None:
        if self.oauth_flow is None:
            self.oauth_flow = OAuthFlow(self._ensure_websession())
        url, state = self.oauth_flow.build_authorize_url()
        self.mqtt.publish_challenge(url, state)
        self.mqtt.publish_auth_status({"status": "login_required", "reason": reason})

    async def _mark_reauth_required(self, error: Exception) -> None:
        if self.reauth_required:
            logger.debug("Reauthentication already requested: %s", self.auth_reason)
            return
        self.authenticated = False
        self.reauth_required = True
        self.auth_reason = str(error)[:240]
        self._connect_generation += 1
        recovery_task = self._recovery_task
        if recovery_task is not None and recovery_task is not asyncio.current_task():
            recovery_task.cancel()
            self._recovery_task = None
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("Error disconnecting stale client")
        data = {
            "transport_connected": False,
            "gateway_status": "AUTH_REQUIRED",
            "vessel_status": "UNKNOWN",
            "is_fully_connected": False,
            "reauth_required": True,
            "auth_status": "reauth_required",
            "auth_reason": self.auth_reason,
            "reauth_reason": self.auth_reason,
        }
        self.mqtt.publish_connectivity(data)
        self.mqtt.publish_auth_status({"status": "reauth_required", "reason": self.auth_reason})
        await self.publish_login_challenge(self.auth_reason)

    def _register_callbacks(self, client: GeckoIotClient, generation: int) -> None:

        def on_zone_update(zones_dict: dict) -> None:
            try:
                serialized = serialize_zones_by_type(zones_dict)
                self.mqtt.publish_snapshot(serialized)
                self.heat_pump.observe_zone_update(zones_dict)
            except Exception:
                logger.exception("Error serializing zone update")

        def on_connectivity_update(connectivity) -> None:
            try:
                data = serialize_connectivity(connectivity)
                data.update({
                    "reauth_required": self.reauth_required,
                    "auth_status": "reauth_required" if self.reauth_required else ("authenticated" if self.authenticated else "unauthenticated"),
                })
                if self.auth_reason:
                    data["auth_reason"] = self.auth_reason
                self.mqtt.publish_connectivity(data)
                if not data.get("is_fully_connected", False):
                    self._schedule_recovery(client, generation)
            except Exception:
                logger.exception("Error serializing connectivity")

        def on_operation_mode_update(controller) -> None:
            try:
                self.mqtt.publish_operation_mode(controller.to_dict())
            except Exception:
                logger.exception("Error serializing operation mode")

        client.on(EventChannel.ZONE_UPDATE, on_zone_update)
        client.on(EventChannel.CONNECTIVITY_UPDATE, on_connectivity_update)
        client.on(EventChannel.OPERATION_MODE_UPDATE, on_operation_mode_update)

    def _schedule_recovery(self, client: GeckoIotClient, generation: int) -> None:
        """Schedule one delayed rebuild from a library callback thread."""
        def create_recovery_task() -> None:
            if self._stop_event.is_set() or self.reauth_required or not self.authenticated:
                logger.debug("Skipping Gecko recovery: stopped, unauthenticated, or reauth required")
                return
            if generation != self._connect_generation or self.client is not client:
                logger.debug("Skipping Gecko recovery for superseded client generation=%s", generation)
                return
            if self._recovery_task is not None and not self._recovery_task.done():
                logger.debug("Gecko recovery already scheduled for generation=%s", generation)
                return
            logger.warning(
                "Scheduling Gecko transport recovery generation=%s connected=%s",
                generation,
                client.is_connected,
            )
            self._recovery_task = self.loop.create_task(
                self._recover_gecko_client(client, generation),
                name="gecko-transport-recovery",
            )

        self.loop.call_soon_threadsafe(create_recovery_task)

    async def _recover_gecko_client(self, client: GeckoIotClient, generation: int) -> None:
        delay = settings.gecko_recovery_delay
        task = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(delay)
                if self._stop_event.is_set():
                    logger.debug("Skipping Gecko recovery after shutdown")
                    return
                if self.reauth_required or not self.authenticated:
                    logger.debug("Skipping Gecko recovery after authentication state changed")
                    return
                if client is not None and (
                    self.client is not client or generation != self._connect_generation
                ):
                    logger.debug("Skipping Gecko recovery for superseded client generation=%s", generation)
                    return
                if client is not None and client.is_connected:
                    logger.info("Skipping Gecko recovery; transport recovered generation=%s", generation)
                    return
                try:
                    logger.warning("Rebuilding Gecko client generation=%s", generation)
                    await self.start_gecko_client()
                    logger.info("Gecko client rebuild started generation=%s", self._connect_generation)
                    return
                except OAuthAuthenticationError as exc:
                    await self._mark_reauth_required(exc)
                    return
                except Exception as exc:
                    next_delay = min(delay * 2, 300.0)
                    logger.error(
                        "Gecko client rebuild failed: %s; retrying in %.0fs",
                        exc,
                        next_delay,
                    )
                    # An external start cancels this task. A failed rebuild leaves no
                    # current client, so the next attempt belongs to this task.
                    client = None
                    generation = self._connect_generation
                    delay = next_delay
        finally:
            if self._recovery_task is task:
                self._recovery_task = None

    def _publish_snapshot_safe(self) -> None:
        try:
            if self.client:
                self.mqtt.publish_snapshot(serialize_zones_by_type(self.client.get_zones()))
                self.mqtt.publish_connectivity(self.get_connectivity_serialized())
                self.mqtt.publish_operation_mode(self.client.operation_mode_controller.to_dict())
        except Exception as exc:
            logger.debug("Snapshot not available yet: %s", exc)

    def get_connectivity_serialized(self) -> dict[str, Any]:
        if self.client:
            data = serialize_connectivity(self.client.connectivity_status)
        else:
            data = {"transport_connected": False, "gateway_status": "UNKNOWN", "vessel_status": "UNKNOWN", "is_fully_connected": False}
        data.update({"reauth_required": self.reauth_required, "auth_status": "reauth_required" if self.reauth_required else ("authenticated" if self.authenticated else "unauthenticated")})
        if self.auth_reason:
            data["auth_reason"] = self.auth_reason
        return data

    def _schedule(self, coroutine) -> None:
        asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def handle_auth_response(self, payload: str) -> None:
        text = payload.strip()
        if not text:
            # An empty message deletes the topic, it is not a login attempt.
            # Reported as an error it would stick the status on
            # `login_required` even though the session is valid.
            logger.debug("Ignoring empty OAuth response")
            return
        try:
            data = json.loads(text) if text.startswith("{") else {"redirect_url": text}
            code, state = self._extract_callback_values(data)
            self._schedule(self._complete_oauth_safe(code, state))
        except Exception as exc:
            logger.error("Invalid OAuth response: %s", exc)
            # Only a genuinely missing session may report `login_required`.
            # Otherwise the topic contradicts the real state and stays wrong
            # until the next connect.
            if self.authenticated:
                self.mqtt.publish_auth_status(
                    {"status": "authenticated", "reason": f"invalid response ignored: {exc}"[:240]}
                )
            else:
                self.mqtt.publish_auth_status({"status": "login_required", "reason": str(exc)})

    async def _complete_oauth_safe(self, code: str, state: str | None) -> None:
        self.mqtt.publish_auth_status({"status": "authenticating", "reason": ""})
        try:
            await self.complete_oauth(code, state)
        except Exception as exc:
            logger.error("OAuth completion failed: %s", exc)
            await self.publish_login_challenge(str(exc))

    def handle_auth_login(self) -> None:
        self._schedule(self.publish_login_challenge("login requested"))

    def handle_command(self, command_type: str, zone_id: str, data: dict[str, Any]) -> None:
        self._schedule(self._handle_command(command_type, zone_id, data))

    def handle_heat_pump_command(self, data: dict[str, Any]) -> None:
        self.heat_pump.handle_command(data)

    async def _handle_command(self, command_type: str, zone_id: str, data: dict[str, Any]) -> None:
        try:
            logger.info("Handling flow/control command type=%s zone_id=%s data=%s", command_type, zone_id, data)
            if not self.authenticated or not self.client:
                raise RuntimeError("Gecko client is not authenticated")
            await self._wait_until_connected()
            logger.info("Gecko client ready for command; connected=%s", self.client.is_connected)
            if command_type == "temperature":
                await asyncio.to_thread(self.set_target_temperature, zone_id, float(data["target_temperature"]))
                message = f"Target temperature set to {data['target_temperature']}"
            elif command_type == "lighting":
                await asyncio.to_thread(self.set_light, zone_id, data.get("action", "on"), int(data.get("r", 255)), int(data.get("g", 255)), int(data.get("b", 255)), data.get("intensity"), data.get("effect"))
                message = "Light command applied"
            elif command_type == "flow":
                await asyncio.to_thread(self.set_flow, zone_id, data.get("action", "off"), data.get("speed"))
                message = "Flow command applied"
            else:
                raise ValueError(f"unsupported command type: {command_type}")
            self.mqtt.publish_result(command_type, zone_id, True, message)
        except Exception as exc:
            logger.error("Command failed: %s", exc)
            self.mqtt.publish_result(command_type, zone_id, False, str(exc))

    async def _wait_until_connected(self, timeout: float | None = None) -> None:
        """Wait for the library's full transport/gateway/vessel readiness."""
        if self.client is None:
            raise RuntimeError("Gecko client is not available")
        if timeout is None:
            timeout = settings.config_timeout
        deadline = self.loop.time() + timeout
        while not self.client.is_connected:
            remaining = deadline - self.loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    "Gecko client is not fully connected (transport/gateway/vessel)"
                )
            await asyncio.sleep(min(0.5, remaining))

    def set_target_temperature(self, zone_id: str, temperature: float) -> None:
        assert self.client is not None
        self.client.get_zone_by_id_and_type(ZoneType.TEMPERATURE_CONTROL_ZONE, zone_id).set_target_temperature(temperature)

    def set_light(self, zone_id: str, action: str, r: int, g: int, b: int, intensity: int | None, effect: str | None) -> None:
        assert self.client is not None
        zone = self.client.get_zone_by_id_and_type(ZoneType.LIGHTING_ZONE, zone_id)
        if action == "off":
            zone.deactivate()
        elif effect:
            zone.set_effect(effect)
        else:
            zone.set_color(r, g, b, intensity)

    def set_flow(self, zone_id: str, action: str, speed: float | None) -> None:
        assert self.client is not None
        zone = self.client.get_zone_by_id_and_type(ZoneType.FLOW_ZONE, zone_id)
        logger.info("Applying flow command zone=%s action=%s speed=%s active=%s initiators=%s", zone_id, action, speed, zone.active, getattr(zone, "initiators", None))
        if action == "on":
            if speed is not None:
                speed_config = zone.speed_config
                if not speed_config or speed_config.get("stepIncrement", 0) == 0:
                    raise RuntimeError(
                        f"Flow zone {zone_id} supports on/off only; speed percentage is not supported"
                    )
                zone.set_speed(speed, active=True)
            else:
                zone.activate()
        else:
            zone.deactivate()

    @staticmethod
    def _extract_callback_values(req: dict[str, Any]) -> tuple[str, str | None]:
        if req.get("code"):
            return req["code"], req.get("state")
        redirect_url = req.get("redirect_url")
        if not redirect_url:
            raise ValueError("Provide either 'code' or the complete 'redirect_url'")
        query = parse_qs(urlsplit(redirect_url).query)
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        wrapped = query.get("redirect", [None])[0]
        if wrapped and not code:
            wrapped_query = parse_qs(urlsplit("http://callback/" + wrapped.lstrip("/")).query)
            code = wrapped_query.get("code", [None])[0]
            state = wrapped_query.get("state", [None])[0]
        if not code:
            raise ValueError("No OAuth code found in redirect_url")
        return code, state

    async def stop(self) -> None:
        await self.heat_pump.stop()
        self._stop_event.set()
        self._connect_generation += 1
        recovery_task = self._recovery_task
        if recovery_task is not None:
            recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await recovery_task
            if self._recovery_task is recovery_task:
                self._recovery_task = None
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("Error disconnecting Gecko client")
        if self.websession:
            await self.websession.close()
