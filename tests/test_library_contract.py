"""Contract test against the installed library.

The ``gecko-iot-client`` library is **not** tested. Its logic is neither
reproduced nor checked for behavior. This test checks only whether the
assumptions ``app/`` makes about the library still hold. It addresses this gap:

``tests/fakes.py`` has its own signatures. If the library changed a call form,
all tests would remain green and the error would appear only at runtime.

The expected values in ``ZONE_METHODS`` and ``CLIENT_SIGNATURES`` were verified
once against 1.0.3. A pin change fails this test and names the deviation
instead of hiding it.
"""

from __future__ import annotations

import importlib.metadata as metadata
import inspect

import pytest
from gecko_iot_client import GeckoIotClient, ZoneType
from gecko_iot_client.models.abstract_zone import AbstractZone
from gecko_iot_client.models.events import EventChannel
from gecko_iot_client.models.flow_zone import FlowZone, FlowZoneInitiator
from gecko_iot_client.models.lighting_zone import LightingZone
from gecko_iot_client.models.temperature_control_zone import TemperatureControlZone
from gecko_iot_client.transporters.mqtt import MqttTransporter

from app.pool_controller import PoolController
from app.zone_serializer import FLOW_INITIATOR_LABELS

from .fakes import FakeZone

#: Version pinned in requirements.txt. A deviation means the contract needs review.
EXPECTED_VERSION = "1.0.3"

#: (Klasse, Methode, erwartete Parameter inkl. self, Aufrufer in app/)
ZONE_METHODS = [
    (FlowZone, "activate", ["self"], "heat_pump_controller.py, pool_controller.py"),
    (FlowZone, "deactivate", ["self"], "heat_pump_controller.py, pool_controller.py"),
    (FlowZone, "set_speed", ["self", "speed", "active"], "pool_controller.py"),
    (LightingZone, "set_color", ["self", "r", "g", "b", "i"], "pool_controller.py"),
    (LightingZone, "set_effect", ["self", "effect_name"], "pool_controller.py"),
    (
        TemperatureControlZone,
        "set_target_temperature",
        ["self", "temperature"],
        "pool_controller.py",
    ),
]

#: Our callers pass no keyword names for set_color, set_effect and
#: set_target_temperature because the library calls them ``i`` and
#: ``effect_name`` there. Only ``speed`` and ``active`` are passed as keywords.
KEYWORD_ARGS = {("set_speed", "active")}

#: Expected signature of the client and transporter APIs called by app/.
CLIENT_SIGNATURES = {
    GeckoIotClient.__init__: ["self", "idd", "transporter", "config_timeout"],
    GeckoIotClient.get_zone_by_id_and_type: ["self", "zone_type", "zone_id"],
    GeckoIotClient.get_zones: ["self"],
    GeckoIotClient.on: ["self", "channel", "callback"],
    MqttTransporter.__init__: [
        "self",
        "broker_url",
        "monitor_id",
        "token_refresh_callback",
        "token_refresh_buffer_seconds",
    ],
}

#: Enum members used by app/.
REQUIRED_EVENT_CHANNELS = [
    "ZONE_UPDATE",
    "CONNECTIVITY_UPDATE",
    "OPERATION_MODE_UPDATE",
]
REQUIRED_ZONE_TYPES = [
    "FLOW_ZONE",
    "TEMPERATURE_CONTROL_ZONE",
    "LIGHTING_ZONE",
]
#: Codes that FLOW_INITIATOR_LABELS maps in app/zone_serializer.py.
REQUIRED_INITIATOR_CODES = ["UD", "CF", "PU", "FI", "HT", "CD", "HTP"]


def parameter_names(func: object) -> list[str]:
    return [p.name for p in inspect.signature(func).parameters.values()]


# --------------------------------------------------------------------------
# Version
# --------------------------------------------------------------------------


def test_tc_contract_installed_version_matches_pin() -> None:
    """The installed library is the pinned version."""
    assert metadata.version("gecko-iot-client") == EXPECTED_VERSION


# --------------------------------------------------------------------------
# Zone methods
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("cls", "method", "expected", "caller"), ZONE_METHODS)
def test_tc_contract_zone_method_signature(cls, method, expected, caller) -> None:
    """The blocking zone method's signature is unchanged.

    If the method is missing entirely, the test already fails while looking it
    up.
    """
    real = getattr(cls, method, None)

    assert real is not None, f"{cls.__name__}.{method} no longer exists"
    actual = parameter_names(real)
    assert actual == expected, (
        f"{cls.__name__}.{method} now has {actual}, expected {expected}. "
        f"Callers: {caller}"
    )


@pytest.mark.parametrize(("cls", "method", "expected", "caller"), ZONE_METHODS)
def test_tc_contract_fake_accepts_what_the_real_method_accepts(
    cls, method, expected, caller
) -> None:
    """The fake accepts what the real method accepts.

    What is checked is the number of parameters and their mandatory status,
    not their names. The library calls the fourth parameter of
    ``set_color`` ``i`` and that of ``set_effect`` ``effect_name``; ``app/``
    calls both positionally, while the fakes use the more descriptive
    ``intensity`` and ``effect``. Keyword arguments are covered by the
    separate test further below.
    """
    real_sig = inspect.signature(getattr(cls, method))
    real_params = [p for n, p in real_sig.parameters.items() if n != "self"]
    real_required = [p for p in real_params if p.default is inspect.Parameter.empty]

    fake_sig = inspect.signature(getattr(FakeZone, method))
    fake_params = [p for n, p in fake_sig.parameters.items() if n != "self"]
    fake_required = [p for p in fake_params if p.default is inspect.Parameter.empty]

    assert len(fake_params) >= len(real_required), (
        f"FakeZone.{method} takes too few parameters for {real_required}"
    )
    assert len(fake_required) <= len(real_required), (
        f"FakeZone.{method} demands more parameters than {cls.__name__}.{method}"
    )


@pytest.mark.parametrize(("cls", "method", "expected", "caller"), ZONE_METHODS)
def test_tc_contract_required_parameters_are_accepted(
    cls, method, expected, caller
) -> None:
    """Every parameter without a default is required by the real method.

    Otherwise our call would raise a ``TypeError`` at runtime without any
    test noticing.
    """
    real = getattr(cls, method)
    required = [
        name
        for name, p in inspect.signature(real).parameters.items()
        if name != "self" and p.default is inspect.Parameter.empty
    ]

    for name in required:
        assert name in expected, (
            f"{cls.__name__}.{method} requires {name!r} without a default, "
            f"the expected contract does not know it. Callers: {caller}"
        )


# --------------------------------------------------------------------------
# Client and transporter
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("func", "expected"), list(CLIENT_SIGNATURES.items()))
def test_tc_contract_client_signature(func, expected) -> None:
    """The client and transporter API called by app/ is unchanged."""
    owner = getattr(func, "__qualname__", str(func)).split(".")[0]
    actual = parameter_names(func)

    assert actual == expected, f"{owner} now has {actual}, expected {expected}"


def test_tc_contract_keyword_names_used_by_app_exist() -> None:
    """The names app/ passes as keywords exist in the library."""
    for method, keyword in KEYWORD_ARGS:
        owner = {
            "set_speed": FlowZone,
        }[method]
        params = inspect.signature(getattr(owner, method)).parameters
        assert keyword in params, f"{method} knows no keyword {keyword!r}"


def test_tc_contract_token_refresh_callback_is_planned_for() -> None:
    """Without token_refresh_callback the transporter plans no reconnect.

    The condition lives in ``MqttTransporter._handle_connection_lost``. If a
    future version drops it, a runtime outage would go unnoticed for good.
    That is why the existence of the parameter is asserted.
    """
    params = inspect.signature(MqttTransporter.__init__).parameters
    assert "token_refresh_callback" in params

    source = inspect.getsource(MqttTransporter._handle_connection_lost)
    assert "token_refresh_callback" in source, (
        "_handle_connection_lost no longer checks the callback, "
        "the reconnect after connection loss would then silently be gone"
    )


# --------------------------------------------------------------------------
# Enum members
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", REQUIRED_EVENT_CHANNELS)
def test_tc_contract_event_channel_member_exists(name) -> None:
    """Every event channel subscribed in app/ exists."""
    assert hasattr(EventChannel, name), f"EventChannel.{name} is missing"


@pytest.mark.parametrize("name", REQUIRED_ZONE_TYPES)
def test_tc_contract_zone_type_member_exists(name) -> None:
    """Every zone type used in app/ exists."""
    assert hasattr(ZoneType, name), f"ZoneType.{name} is missing"


def test_tc_contract_initiator_codes_match_labels() -> None:
    """The codes documented in FLOW_INITIATOR_LABELS exist.

    ``FLOW_INITIATOR_LABELS`` is resolved against
    ``FlowZoneInitiator.value``. A renamed code would silently become
    ``unknown:<code>``.
    """
    library_codes = {member.value for member in FlowZoneInitiator}

    assert set(FLOW_INITIATOR_LABELS) == set(REQUIRED_INITIATOR_CODES)
    assert set(FLOW_INITIATOR_LABELS) <= library_codes, (
        f"Unknown codes: {sorted(set(FLOW_INITIATOR_LABELS) - library_codes)}"
    )


def test_tc_contract_zone_classes_are_distinct() -> None:
    """The three zone types stay distinguishable for the isinstance check.

    ``serialize_zone`` branches on ``isinstance``. Inherited alias types
    would silently change the order of the checks.
    """
    classes = [FlowZone, LightingZone, TemperatureControlZone]
    for cls in classes:
        assert issubclass(cls, AbstractZone)
    assert len(set(classes)) == 3
    assert not issubclass(FlowZone, LightingZone)
    assert not issubclass(LightingZone, TemperatureControlZone)


def test_tc_contract_pool_controller_helpers_exist() -> None:
    """The bridge helpers the tests call directly exist."""
    for name in ("_extract_callback_values", "set_flow", "set_light", "set_target_temperature"):
        assert hasattr(PoolController, name), f"PoolController.{name} is missing"
