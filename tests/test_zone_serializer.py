"""Requirements R-SER: zone serialization (TC-SER-01 through TC-SER-14).

``zone_serializer`` checks zone types with ``isinstance``. The tests replace
diese types with fakes so our mapping logic is tested rather than the library's
class structure. ``ZoneType`` and ``FlowZoneInitiator`` come from the library
because they are real keys
beziehungsweise Codes liefern.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from gecko_iot_client import ZoneType
from gecko_iot_client.models.flow_zone import FlowZoneInitiator

from app import zone_serializer
from app.zone_serializer import (
    FLOW_INITIATOR_LABELS,
    serialize_connectivity,
    serialize_zone,
    serialize_zones_by_type,
)

from .fakes import (
    FakeFlowZone,
    FakeLightingZone,
    FakeTemperatureControlZone,
    FakeUnknownZone,
    FakeZoneType,
)


class FakeRgbi:
    def __init__(self, data: dict[str, int]) -> None:
        self._data = data

    def model_dump(self) -> dict[str, int]:
        return self._data


@pytest.fixture(autouse=True)
def patched_zone_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zone_serializer, "TemperatureControlZone", FakeTemperatureControlZone)
    monkeypatch.setattr(zone_serializer, "LightingZone", FakeLightingZone)
    monkeypatch.setattr(zone_serializer, "FlowZone", FakeFlowZone)


# --------------------------------------------------------------------------
# R-SER-01 Temperaturzone
# --------------------------------------------------------------------------


def test_tc_ser_01_temperature_zone_is_mapped() -> None:
    """R-SER-01: Temperaturzone mit aktuellem und Zielwert."""
    zone = FakeTemperatureControlZone("zone-1", name="Pool", zone_type=ZoneType.TEMPERATURE_CONTROL_ZONE)
    zone.temperature = 27.5
    zone.target_temperature = 28.0
    zone.status = SimpleNamespace(name="HEATING")
    zone.mode = SimpleNamespace(eco=True)
    zone.min_temperature_set_point_c_value = 15.0
    zone.max_temperature_set_point_c_value = 40.0

    result = serialize_zone(zone)

    assert result == {
        "id": "zone-1",
        "name": "Pool",
        "type": "temperature",
        "state": {
            "current_temperature": 27.5,
            "target_temperature": 28.0,
            "status": "HEATING",
            "eco_mode": True,
            "min_set_point": 15.0,
            "max_set_point": 40.0,
        },
    }


def test_tc_ser_01_missing_status_and_mode_yield_none() -> None:
    """R-SER-01: Fehlender Status und Modus ergeben null."""
    zone = FakeTemperatureControlZone(zone_type=ZoneType.TEMPERATURE_CONTROL_ZONE)
    zone.status = None
    zone.mode = None

    state = serialize_zone(zone)["state"]

    assert state["status"] is None
    assert state["eco_mode"] is None


# --------------------------------------------------------------------------
# R-SER-02 Lichtzone
# --------------------------------------------------------------------------


def test_tc_ser_02_lighting_zone_is_mapped() -> None:
    """R-SER-02: Lichtzone mit Aktivitaet, Farbe und Effekt."""
    zone = FakeLightingZone("l-1", name="LED", zone_type=ZoneType.LIGHTING_ZONE)
    zone.active = True
    zone.rgbi = FakeRgbi({"red": 255, "green": 0, "blue": 40})
    zone.effect = "rainbow"

    state = serialize_zone(zone)["state"]

    assert state == {"active": True, "color": {"red": 255, "green": 0, "blue": 40}, "effect": "rainbow"}


def test_tc_ser_02_lighting_zone_without_colour() -> None:
    """R-SER-02: Ohne gesetzte Farbe ist color null."""
    zone = FakeLightingZone(zone_type=ZoneType.LIGHTING_ZONE)
    zone.rgbi = None

    assert serialize_zone(zone)["state"]["color"] is None


# --------------------------------------------------------------------------
# R-SER-03 Flowzone
# --------------------------------------------------------------------------


def test_tc_ser_03_flow_zone_is_mapped() -> None:
    """R-SER-03: Flowzone mit Zustand, Initiatoren und Faehigkeiten."""
    zone = FakeFlowZone("4", name="Pump 4", zone_type=ZoneType.FLOW_ZONE)
    zone.active = True
    zone.speed = 50
    zone.initiators = [FlowZoneInitiator.FILTRATION]
    zone.speed_config = {"minimum": 20, "maximum": 100, "stepIncrement": 10}
    zone.capabilities = ["supports_turn_on", "supports_turn_off"]
    zone.presets = [SimpleNamespace(name="Eco", speed=40)]

    result = serialize_zone(zone)

    assert result["type"] == "flow"
    assert result["state"] == {
        "active": True,
        "speed": 50,
        "initiators": ["FI"],
        "initiator_labels": ["filtration"],
        "capabilities": ["supports_turn_off", "supports_turn_on"],
        "supports_speed_percentage": True,
        "supports_turn_on": True,
        "supports_turn_off": True,
        "speed_config": {"minimum": 20, "maximum": 100, "stepIncrement": 10},
        "presets": [{"name": "Eco", "speed": 40}],
    }


def test_tc_ser_03_multiple_initiators_are_preserved() -> None:
    """R-SER-03: Mehrere gleichzeitige Ursachen werden uebertragen."""
    zone = FakeFlowZone(zone_type=ZoneType.FLOW_ZONE)
    zone.initiators = [FlowZoneInitiator.HEATING, FlowZoneInitiator.FILTRATION]

    state = serialize_zone(zone)["state"]

    assert state["initiators"] == ["HT", "FI"]
    assert state["initiator_labels"] == ["heating", "filtration"]


def test_tc_ser_03_presetless_zone_yields_empty_presets() -> None:
    """R-SER-03: Ohne Presets bleibt die Liste leer."""
    zone = FakeFlowZone(zone_type=ZoneType.FLOW_ZONE)
    zone.presets = []

    assert serialize_zone(zone)["state"]["presets"] == []


# --------------------------------------------------------------------------
# R-SER-04 Initiator-Labels
# --------------------------------------------------------------------------


def test_tc_ser_04_unknown_initiator_code_is_labelled() -> None:
    """R-SER-04: Unbekannte Codes bleiben erhalten und werden markiert."""
    zone = FakeFlowZone(zone_type=ZoneType.FLOW_ZONE)
    zone.initiators = ["ZZ", "FI"]

    state = serialize_zone(zone)["state"]

    assert state["initiators"] == ["ZZ", "FI"]
    assert state["initiator_labels"] == ["unknown:ZZ", "filtration"]


def test_tc_ser_04_all_documented_codes_have_labels() -> None:
    """R-SER-04: Every code documented in the README is covered."""
    assert FLOW_INITIATOR_LABELS == {
        "UD": "user_demand",
        "CF": "checkflow",
        "PU": "purge",
        "FI": "filtration",
        "HT": "heating",
        "CD": "cooldown",
        "HTP": "heat_pump",
    }


def test_tc_ser_04_missing_initiators_yield_empty_lists() -> None:
    """R-SER-04: Ohne Initiatoren bleiben beide Listen leer."""
    zone = FakeFlowZone(zone_type=ZoneType.FLOW_ZONE)
    zone.initiators = None

    state = serialize_zone(zone)["state"]

    assert state["initiators"] == []
    assert state["initiator_labels"] == []


# --------------------------------------------------------------------------
# R-SER-05, R-SER-06 Faehigkeiten und Gruppierung
# --------------------------------------------------------------------------


def test_tc_ser_05_speed_percentage_requires_step_increment() -> None:
    """R-SER-05: supports_speed_percentage ist nur bei stepIncrement ungleich 0 wahr."""
    zone = FakeFlowZone(zone_type=ZoneType.FLOW_ZONE)
    zone.speed_config = {"stepIncrement": 0}

    assert serialize_zone(zone)["state"]["supports_speed_percentage"] is False


def test_tc_ser_05_zone_without_speed_config_is_not_regulable() -> None:
    """R-SER-05: Without speed_config the zone is not adjustable."""
    zone = FakeFlowZone(zone_type=ZoneType.FLOW_ZONE)
    zone.speed_config = None

    assert serialize_zone(zone)["state"]["supports_speed_percentage"] is False


def test_tc_ser_06_capabilities_are_sorted() -> None:
    """R-SER-06: Faehigkeiten werden als Liste von Werten sortiert ausgegeben."""
    zone = FakeFlowZone(zone_type=ZoneType.FLOW_ZONE)
    zone.capabilities = [SimpleNamespace(value="supports_turn_off"), SimpleNamespace(value="supports_turn_on")]

    state = serialize_zone(zone)["state"]

    assert state["capabilities"] == ["supports_turn_off", "supports_turn_on"]
    assert state["supports_turn_on"] is True
    assert state["supports_turn_off"] is True


def test_tc_ser_06_zones_by_type_are_grouped_by_label() -> None:
    """R-SER-06: The grouping uses temperature, lighting and flow."""
    zones = {
        ZoneType.TEMPERATURE_CONTROL_ZONE: [FakeTemperatureControlZone("t1", zone_type=ZoneType.TEMPERATURE_CONTROL_ZONE)],
        ZoneType.LIGHTING_ZONE: [FakeLightingZone("l1", zone_type=ZoneType.LIGHTING_ZONE)],
        ZoneType.FLOW_ZONE: [FakeFlowZone("f1", zone_type=ZoneType.FLOW_ZONE)],
    }

    result = serialize_zones_by_type(zones)

    assert set(result) == {"temperature", "lighting", "flow"}
    assert result["flow"][0]["id"] == "f1"


def test_tc_ser_06_unknown_zone_type_falls_back_to_value() -> None:
    """R-SER-06: An unknown zone type uses its own value."""
    unknown = FakeZoneType("custom")
    zones = {unknown: [FakeUnknownZone("x1", zone_type=unknown)]}

    result = serialize_zones_by_type(zones)

    assert list(result) == ["custom"]
    assert result["custom"][0]["type"] == "custom"


# --------------------------------------------------------------------------
# R-SER-07 Unbekannte Zonen
# --------------------------------------------------------------------------


def test_tc_ser_07_unknown_zone_type_yields_empty_state() -> None:
    """R-SER-07: An unknown zone gets an empty state."""
    zone = FakeUnknownZone("x1", name="Spaet", zone_type=FakeZoneType("custom"))

    assert serialize_zone(zone) == {"id": "x1", "name": "Spaet", "type": "custom", "state": {}}


# --------------------------------------------------------------------------
# R-SER-08 Connectivity
# --------------------------------------------------------------------------


def test_tc_ser_08_connectivity_uses_to_dict() -> None:
    """R-SER-08: Connectivity with to_dict is taken over directly."""
    connectivity = SimpleNamespace(to_dict=lambda: {"is_fully_connected": True})

    assert serialize_connectivity(connectivity) == {"is_fully_connected": True}


def test_tc_ser_08_connectivity_without_to_dict_is_wrapped() -> None:
    """R-SER-08: Objekte ohne to_dict werden als roher Wert ausgegeben."""
    assert serialize_connectivity("verbunden") == {"raw": "verbunden"}


def test_tc_ser_08_connectivity_none_is_wrapped() -> None:
    """R-SER-08: None is tolerated as well."""
    assert serialize_connectivity(None) == {"raw": "None"}


def test_tc_ser_01_zone_type_defaults_to_english_label() -> None:
    """R-SER-01: The type label is the English hyphenated name."""
    zone = FakeTemperatureControlZone(zone_type=ZoneType.TEMPERATURE_CONTROL_ZONE)

    assert serialize_zone(zone)["type"] == "temperature"


@pytest.mark.parametrize(
    ("zone_class", "zone_type", "expected"),
    [
        (FakeFlowZone, ZoneType.FLOW_ZONE, "flow"),
        (FakeLightingZone, ZoneType.LIGHTING_ZONE, "lighting"),
        (FakeTemperatureControlZone, ZoneType.TEMPERATURE_CONTROL_ZONE, "temperature"),
    ],
)
def test_tc_ser_01_every_zone_type_has_a_label(zone_class: Any, zone_type: Any, expected: str) -> None:
    """R-SER-01: Jeder unterstuetzte Zonentyp hat eine Bezeichnung."""
    zone = zone_class(zone_type=zone_type)

    assert serialize_zone(zone)["type"] == expected
