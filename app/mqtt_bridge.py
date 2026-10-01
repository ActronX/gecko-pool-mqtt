from __future__ import annotations

import json
import logging
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .config import settings
from .mqtt_trace import MqttTrace

logger = logging.getLogger(__name__)


class MqttBridge:
    """Local MQTT transport for status publication and command dispatch."""

    def __init__(
        self,
        on_command: Callable[[str, str, dict[str, Any]], None],
        on_heat_pump: Callable[[dict[str, Any]], None],
        on_auth_response: Callable[[str], None],
        on_auth_login: Callable[[], None],
    ) -> None:
        self.on_command = on_command
        self.on_heat_pump = on_heat_pump
        self.on_auth_response = on_auth_response
        self.on_auth_login = on_auth_login
        self.base = settings.mqtt_base_topic.rstrip("/")
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
        )
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password or None)
        self.client.will_set(
            self.topic("status/availability"), payload="offline", qos=1, retain=True
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.trace = MqttTrace(settings.mqtt_trace_dir, settings.mqtt_trace_retention_days)

    def topic(self, suffix: str) -> str:
        return f"{self.base}/{suffix.lstrip('/')}"

    def start(self) -> None:
        self.client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self._publish_and_wait("status/availability", "offline", qos=1, retain=True)
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            self.trace.close()

    def _publish_and_wait(self, suffix: str, payload: Any, qos: int, retain: bool) -> None:
        """Publish and block until the broker acknowledges or the timeout elapses."""
        info = self.publish(suffix, payload, qos=qos, retain=retain)
        if info is None:
            return
        try:
            info.wait_for_publish(timeout=settings.mqtt_shutdown_publish_timeout)
        except (ValueError, RuntimeError) as exc:
            logger.warning("MQTT shutdown publish for %s failed: %s", suffix, exc)
            return
        if not info.is_published():
            logger.warning(
                "MQTT shutdown publish for %s was not acknowledged within %.1fs",
                suffix,
                settings.mqtt_shutdown_publish_timeout,
            )

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT connection rejected: %s", reason_code)
            return
        logger.info("Connected to local MQTT broker %s:%s", settings.mqtt_host, settings.mqtt_port)
        client.subscribe([
            (self.topic("cmd/+/+/set"), 1),
            (self.topic("cmd/heatPump"), 1),
            (self.topic("auth/response"), 1),
            (self.topic("auth/login"), 1),
        ])
        logger.info("Subscribed to local command topics under %s", self.base)
        self.publish("status/availability", "online", qos=1, retain=True)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, disconnect_flags: Any, reason_code: Any, properties: Any = None) -> None:
        if getattr(reason_code, "is_failure", False):
            logger.warning("Disconnected from MQTT broker: %s", reason_code)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        topic = message.topic
        payload = message.payload.decode("utf-8", errors="replace")
        self.trace.record("in", topic, payload)
        logger.info("Received local MQTT message topic=%s payload=%s", topic, payload)
        try:
            if topic == self.topic("auth/response"):
                self.on_auth_response(payload)
                return
            if topic == self.topic("auth/login"):
                self.on_auth_login()
                return
            if topic == self.topic("cmd/heatPump"):
                data = json.loads(payload) if payload.strip() else {}
                if not isinstance(data, dict):
                    raise ValueError("heatPump payload must be a JSON object")
                self._validate_heat_pump_command(data)
                logger.info("Dispatching heatPump command data=%s", data)
                self.on_heat_pump(data)
                return
            # topic("cmd/") already retains the trailing slash.
            prefix = self.topic("cmd/")
            if topic.startswith(prefix) and topic.endswith("/set"):
                parts = topic[len(prefix):].split("/")
                if len(parts) != 3 or parts[2] != "set":
                    return
                command_type, zone_id, _ = parts
                data = json.loads(payload) if payload.strip() else {}
                if not isinstance(data, dict):
                    raise ValueError("command payload must be a JSON object")
                self._validate_command(command_type, data)
                logger.info("Dispatching command type=%s zone_id=%s data=%s", command_type, zone_id, data)
                self.on_command(command_type, zone_id, data)
        except Exception as exc:
            logger.error("Invalid MQTT message on %s: %s", topic, exc)
            if topic == self.topic("cmd/heatPump"):
                self.publish("cmd/heatPump/result", {
                    "success": False,
                    "message": str(exc),
                }, qos=1, retain=False)
                return
            if topic.startswith(self.topic("cmd/")):
                parts = topic.split("/")
                if len(parts) >= 4:
                    self.publish(f"cmd/{parts[-3]}/{parts[-2]}/result", {
                        "success": False, "message": str(exc), "zone_id": parts[-2]
                    }, qos=1, retain=False)

    @staticmethod
    def _validate_command(command_type: str, data: dict[str, Any]) -> None:
        if command_type == "temperature":
            if not isinstance(data.get("target_temperature"), (int, float)):
                raise ValueError("target_temperature must be numeric")
        elif command_type == "lighting":
            if data.get("action") not in {"on", "off"}:
                raise ValueError("action must be on or off")
            if data.get("action") == "off" and "effect" in data:
                raise ValueError("effect cannot be used with lighting action off")
            for key in ("r", "g", "b", "intensity"):
                if key in data and (not isinstance(data[key], int) or not 0 <= data[key] <= 255):
                    raise ValueError(f"{key} must be an integer from 0 to 255")
        elif command_type == "flow":
            if data.get("action") not in {"on", "off"}:
                raise ValueError("action must be on or off")
            if "speed" in data and (
                isinstance(data["speed"], bool)
                or not isinstance(data["speed"], (int, float))
            ):
                raise ValueError("speed must be numeric")
        else:
            raise ValueError(f"unsupported command type: {command_type}")

    @staticmethod
    def _validate_heat_pump_command(data: dict[str, Any]) -> None:
        action = data.get("action")
        if action not in {"on", "off"}:
            raise ValueError("heatPump action must be on or off")
        if action == "on" and "duration" in data:
            duration = data["duration"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise ValueError("heatPump duration must be numeric minutes")
            if not float(duration).is_integer() or duration <= 0:
                raise ValueError("heatPump duration must be a positive whole number of minutes")

    def publish(self, suffix: str, payload: Any, qos: int = 1, retain: bool = True) -> mqtt.MQTTMessageInfo | None:
        value = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        topic = self.topic(suffix)
        result = self.client.publish(topic, value, qos=qos, retain=retain)
        # Failed sends also go into the trace; otherwise the messages
        # indicating a broker or Gecko error would be missing. `value` is the
        # serialized payload, so the file and broker show the same thing.
        # This preserves evidence of failed sends for diagnosis.
        self.trace.record("out", topic, value, qos=qos, retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("MQTT publish failed for %s: %s", suffix, result.rc)
            return None
        return result

    def publish_zone(self, zone_type: str, zone_id: str, payload: dict[str, Any]) -> None:
        self.publish(f"status/zone/{zone_type}/{zone_id}", payload)

    def publish_connectivity(self, payload: dict[str, Any]) -> None:
        self.publish("status/connectivity", payload)

    def publish_operation_mode(self, payload: dict[str, Any]) -> None:
        self.publish("status/operation_mode", payload)

    def publish_snapshot(self, zones: dict[str, Any]) -> None:
        for zone_type, zone_list in zones.items():
            for zone in zone_list:
                self.publish_zone(zone_type, str(zone["id"]), zone)
        self.publish("status/zones", zones)

    def publish_auth_status(self, payload: dict[str, Any]) -> None:
        self.publish("auth/status", payload)

    def publish_challenge(self, authorize_url: str, state: str) -> None:
        self.publish("auth/challenge", {
            "authorize_url": authorize_url,
            "state": state,
            "instructions": "Open authorize_url, complete login, then publish the full redirect URL to geeko/auth/response.",
        })

    def clear_challenge(self) -> None:
        self.publish("auth/challenge", "", qos=1, retain=True)

    def publish_result(self, command_type: str, zone_id: str, success: bool, message: str) -> None:
        self.publish(f"cmd/{command_type}/{zone_id}/result", {
            "success": success, "message": message, "zone_id": zone_id
        }, qos=1, retain=False)

    def publish_heat_pump_result(self, success: bool, message: str, **details: Any) -> None:
        payload = {"success": success, "message": message, **details}
        self.publish("cmd/heatPump/result", payload, qos=1, retain=False)

    def publish_heat_pump_reassert(self, payload: dict[str, Any]) -> None:
        self.publish("status/heatPump/reassert", payload, qos=1, retain=False)

    def publish_heat_pump_error(self, payload: dict[str, Any]) -> None:
        self.publish("status/heatPump/error", payload, qos=1, retain=False)

    def publish_heat_pump_state(self, payload: dict[str, Any]) -> None:
        self.publish("status/heatPump/state", payload, qos=1, retain=True)
