"""Requirements R-CFG: configuration and validation (TC-CFG-01 through TC-CFG-08)."""

from __future__ import annotations

import pytest

from app.config import Settings

ENV_KEYS = (
    "GECKO_AUTH0_URL_BASE",
    "GECKO_API_BASE_URL",
    "GECKO_OAUTH2_CLIENT_ID",
    "OAUTH_REDIRECT_URI",
    "OAUTH_TOKEN_FILE",
    "OAUTH_SCOPE",
    "OAUTH_AUDIENCE",
    "GECKO_ACCOUNT_ID",
    "GECKO_MONITOR_ID",
    "LOG_LEVEL",
    "GECKO_CONFIG_TIMEOUT",
    "GECKO_HEAT_PUMP_FLOW_ZONE_ID",
    "GECKO_HEAT_PUMP_DEFAULT_DURATION",
    "GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS",
    "GECKO_HEAT_PUMP_CHECK_INTERVAL",
    "GECKO_HEAT_PUMP_CONFIRM_TIMEOUT",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_BASE_TOPIC",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "MQTT_CLIENT_ID",
    "MQTT_SHUTDOWN_PUBLISH_TIMEOUT",
    "MQTT_TRACE_DIR",
    "MQTT_TRACE_RETENTION_DAYS",
)


def build(env, **overrides) -> Settings:
    """Creates fresh settings with an empty environment and the given values."""
    for key in ENV_KEYS:
        env(key, None)
    for key, value in overrides.items():
        env(key, str(value))
    return Settings()


def test_tc_cfg_01_defaults_without_environment(env) -> None:
    """R-CFG-01: The documented default values apply without an environment."""
    config = build(env)

    assert config.oauth_client_id == "L81oh6hgUsvMg40TgTGoz4lxNy8eViM0"
    assert config.oauth_redirect_uri == "https://my.home-assistant.io/redirect/oauth"
    assert config.oauth_token_file == "/data/tokens.json"
    assert config.mqtt_host == "mqtt.example.com"
    assert config.mqtt_port == 1883
    assert config.mqtt_base_topic == "gecko"
    assert config.mqtt_client_id == "gecko-pool-mqtt"
    assert config.mqtt_shutdown_publish_timeout == 2.0
    assert config.config_timeout == 30.0
    assert config.heat_pump_flow_zone_id == "4"
    assert config.heat_pump_default_duration == 30
    assert config.heat_pump_max_reassert_attempts == 2
    assert config.heat_pump_check_interval == 5.0
    assert config.heat_pump_confirm_timeout == 15.0
    assert config.mqtt_trace_dir == "/tmp/mqtt"
    assert config.mqtt_trace_retention_days == 7


def test_tc_cfg_01_values_are_read_from_environment(env) -> None:
    """R-CFG-01: Every setting comes from the environment."""
    config = build(
        env,
        MQTT_BASE_TOPIC="pool",
        MQTT_CLIENT_ID="client-x",
        GECKO_HEAT_PUMP_FLOW_ZONE_ID="9",
        GECKO_HEAT_PUMP_CONFIRM_TIMEOUT="42.5",
        OAUTH_TOKEN_FILE="/tmp/t.json",
    )

    assert config.mqtt_base_topic == "pool"
    assert config.mqtt_client_id == "client-x"
    assert config.heat_pump_flow_zone_id == "9"
    assert config.heat_pump_confirm_timeout == 42.5
    assert config.oauth_token_file == "/tmp/t.json"


def test_tc_cfg_02_missing_oauth_client_id_is_reported(env) -> None:
    """R-CFG-02: A missing OAuth client ID is reported."""
    errors = build(env, GECKO_OAUTH2_CLIENT_ID="").validate()

    assert "GECKO_OAUTH2_CLIENT_ID is required" in errors


def test_tc_cfg_02_missing_mqtt_host_is_reported(env) -> None:
    """R-CFG-02: A missing broker host is reported."""
    errors = build(env, MQTT_HOST="").validate()

    assert "MQTT_HOST is required" in errors


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("GECKO_HEAT_PUMP_DEFAULT_DURATION", 0, "GECKO_HEAT_PUMP_DEFAULT_DURATION must be positive"),
        ("GECKO_HEAT_PUMP_CHECK_INTERVAL", 0, "GECKO_HEAT_PUMP_CHECK_INTERVAL must be positive"),
        ("GECKO_HEAT_PUMP_CONFIRM_TIMEOUT", 0, "GECKO_HEAT_PUMP_CONFIRM_TIMEOUT must be positive"),
        (
            "GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS",
            -1,
            "GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS must be zero or positive",
        ),
    ],
)
def test_tc_cfg_03_invalid_heat_pump_timings_are_reported(env, key, value, message) -> None:
    """R-CFG-03: Invalid heat-pump values are reported."""
    errors = build(env, **{key: value}).validate()

    assert message in errors


def test_tc_cfg_04_confirm_timeout_below_check_interval_is_reported(env) -> None:
    """R-CFG-04: CONFIRM_TIMEOUT must be at least CHECK_INTERVAL."""
    errors = build(
        env, GECKO_HEAT_PUMP_CHECK_INTERVAL=10.0, GECKO_HEAT_PUMP_CONFIRM_TIMEOUT=5.0
    ).validate()

    assert "GECKO_HEAT_PUMP_CONFIRM_TIMEOUT must be at least GECKO_HEAT_PUMP_CHECK_INTERVAL" in errors


def test_tc_cfg_04_confirm_timeout_covers_the_mutation_block(env) -> None:
    """R-CFG-04: CONFIRM_TIMEOUT must cover the blocking PUBACK wait.

    A reassert can remain in the call for 5 seconds. With a smaller window,
    the deadline expires during the call.
    """
    errors = build(
        env, GECKO_HEAT_PUMP_CHECK_INTERVAL=1.0, GECKO_HEAT_PUMP_CONFIRM_TIMEOUT=2.0
    ).validate()

    assert any(
        "CONFIRM_TIMEOUT must be at least 5s" in message for message in errors
    ), errors


def test_tc_cfg_04_confirm_timeout_exactly_the_block_is_valid(env) -> None:
    """R-CFG-04: Exactly 5 seconds is still valid."""
    assert build(
        env, GECKO_HEAT_PUMP_CHECK_INTERVAL=1.0, GECKO_HEAT_PUMP_CONFIRM_TIMEOUT=5.0
    ).validate() == []


def test_tc_cfg_05_valid_configuration_has_no_errors(env) -> None:
    """R-CFG-05: A valid configuration reports no errors."""
    assert build(env).validate() == []


def test_tc_cfg_06_negative_shutdown_publish_timeout_is_reported(env) -> None:
    """R-CFG-06: A negative shutdown wait is reported."""
    errors = build(env, MQTT_SHUTDOWN_PUBLISH_TIMEOUT=-1.0).validate()

    assert "MQTT_SHUTDOWN_PUBLISH_TIMEOUT must be zero or positive" in errors


def test_tc_cfg_06_zero_shutdown_publish_timeout_is_allowed(env) -> None:
    """R-CFG-06: Zero is allowed and disables waiting."""
    assert build(env, MQTT_SHUTDOWN_PUBLISH_TIMEOUT=0.0).validate() == []


def test_tc_cfg_05_zero_max_attempts_is_valid(env) -> None:
    """R-CFG-03: Zero reassert attempts means emergency stop is off and is valid."""
    assert build(env, GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS=0).validate() == []


# --------------------------------------------------------------------------
# R-CFG-08 MQTT trace
# --------------------------------------------------------------------------


def test_tc_cfg_08_trace_values_are_read_from_environment(env) -> None:
    """R-CFG-08: The directory and retention come from the environment."""
    config = build(env, MQTT_TRACE_DIR="/var/log/gecko", MQTT_TRACE_RETENTION_DAYS="30")

    assert config.mqtt_trace_dir == "/var/log/gecko"
    assert config.mqtt_trace_retention_days == 30


@pytest.mark.parametrize("value", ["", "off", "OFF"])
def test_tc_cfg_08_disabled_trace_is_valid(env, value: str) -> None:
    """R-CFG-08: Empty or off disables the trace without errors."""
    assert build(env, MQTT_TRACE_DIR=value).validate() == []


def test_tc_cfg_08_zero_retention_is_valid(env) -> None:
    """R-CFG-08: Zero days means unlimited retention."""
    assert build(env, MQTT_TRACE_RETENTION_DAYS=0).validate() == []


def test_tc_cfg_08_negative_retention_is_reported(env) -> None:
    """R-CFG-08: Negative retention is reported."""
    errors = build(env, MQTT_TRACE_RETENTION_DAYS=-1).validate()

    assert "MQTT_TRACE_RETENTION_DAYS must be zero or positive" in errors


def test_tc_cfg_08_relative_trace_dir_is_reported(env) -> None:
    """R-CFG-08: A relative path is reported because it misleads in the container.

    Without a bind mount, a relative directory lands in the container's
    writable layer and cannot be reached from Windows.
    """
    errors = build(env, MQTT_TRACE_DIR="mqtt-trace").validate()

    assert "MQTT_TRACE_DIR must be an absolute path or 'off'" in errors
