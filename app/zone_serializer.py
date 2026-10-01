from __future__ import annotations

import logging
from typing import Any

from gecko_iot_client import AbstractZone, ZoneType
from gecko_iot_client.models.flow_zone import FlowZone
from gecko_iot_client.models.lighting_zone import LightingZone
from gecko_iot_client.models.temperature_control_zone import TemperatureControlZone

logger = logging.getLogger(__name__)

ZONE_TYPE_LABELS = {
    ZoneType.TEMPERATURE_CONTROL_ZONE: "temperature",
    ZoneType.LIGHTING_ZONE: "lighting",
    ZoneType.FLOW_ZONE: "flow",
}

# Gecko reports the reason a flow zone is active as short initiator codes.
# Keep the codes in the MQTT payload and add readable labels for consumers.
FLOW_INITIATOR_LABELS = {
    "UD": "user_demand",
    "CF": "checkflow",
    "PU": "purge",
    "FI": "filtration",
    "HT": "heating",
    "CD": "cooldown",
    "HTP": "heat_pump",
}


def _serialize_flow_initiators(zone: FlowZone) -> tuple[list[str], list[str]]:
    codes: list[str] = []
    labels: list[str] = []
    for initiator in zone.initiators or []:
        code = getattr(initiator, "value", initiator)
        code = str(code)
        codes.append(code)
        labels.append(FLOW_INITIATOR_LABELS.get(code, f"unknown:{code}"))
    return codes, labels


def serialize_zone(zone: AbstractZone) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": zone.id,
        "name": zone.name,
        "type": ZONE_TYPE_LABELS.get(zone.zone_type, zone.zone_type.value),
    }

    if isinstance(zone, TemperatureControlZone):
        result["state"] = {
            "current_temperature": zone.temperature,
            "target_temperature": zone.target_temperature,
            "status": zone.status.name if zone.status else None,
            "eco_mode": zone.mode.eco if zone.mode else None,
            "min_set_point": zone.min_temperature_set_point_c_value,
            "max_set_point": zone.max_temperature_set_point_c_value,
        }
    elif isinstance(zone, LightingZone):
        result["state"] = {
            "active": zone.active,
            "color": zone.rgbi.model_dump() if zone.rgbi else None,
            "effect": zone.effect,
        }
    elif isinstance(zone, FlowZone):
        initiators, initiator_labels = _serialize_flow_initiators(zone)
        speed_config = zone.speed_config
        capability_values = {
            getattr(capability, "value", str(capability))
            for capability in zone.capabilities
        }
        supports_speed_percentage = bool(
            speed_config and speed_config.get("stepIncrement", 0) != 0
        )
        result["state"] = {
            "active": zone.active,
            "speed": zone.speed,
            "initiators": initiators,
            "initiator_labels": initiator_labels,
            "capabilities": sorted(capability_values),
            "supports_speed_percentage": supports_speed_percentage,
            "supports_turn_on": "supports_turn_on" in capability_values,
            "supports_turn_off": "supports_turn_off" in capability_values,
            "speed_config": speed_config,
            "presets": [{"name": p.name, "speed": p.speed} for p in zone.presets],
        }
    else:
        result["state"] = {}

    return result


def serialize_zones_by_type(zones_dict: dict[ZoneType, list[AbstractZone]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for zone_type, zone_list in zones_dict.items():
        label = ZONE_TYPE_LABELS.get(zone_type, zone_type.value)
        output[label] = [serialize_zone(z) for z in zone_list]
    return output


def serialize_connectivity(connectivity) -> dict[str, Any]:
    if hasattr(connectivity, "to_dict"):
        return connectivity.to_dict()
    return {"raw": str(connectivity)}
