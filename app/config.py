import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

#: Blocking zone methods from gecko-iot-client 1.0.3 wait up to this many
#: seconds for the broker's PUBACK confirmation. The value depends on the
#: library, not on us; tests/test_library_contract.py pins the version.
GECKO_MUTATION_BLOCK_SECONDS = 5.0


@dataclass
class Settings:
    # --- Gecko / OAuth (identical to reference) ---
    auth0_url: str = field(default_factory=lambda: os.getenv(
        "GECKO_AUTH0_URL_BASE", "https://gecko-prod.us.auth0.com"
    ))
    api_url: str = field(default_factory=lambda: os.getenv(
        "GECKO_API_BASE_URL", "https://api.geckowatermonitor.com"
    ))
    #: Public PKCE client of the Gecko Alliance, without a client secret. Not a
    #: secret; deliberately the default so the service runs without an extra step.
    oauth_client_id: str = field(default_factory=lambda: os.getenv(
        "GECKO_OAUTH2_CLIENT_ID", "L81oh6hgUsvMg40TgTGoz4lxNy8eViM0"
    ))
    oauth_redirect_uri: str = field(default_factory=lambda: os.getenv(
        "OAUTH_REDIRECT_URI", "https://my.home-assistant.io/redirect/oauth"
    ))
    oauth_token_file: str = field(default_factory=lambda: os.getenv(
        "OAUTH_TOKEN_FILE", "/data/tokens.json"
    ))
    oauth_scope: str = field(default_factory=lambda: "openid profile email offline_access")
    oauth_audience: str = field(default_factory=lambda: "https://api.geckowatermonitor.com")
    account_id: str = field(default_factory=lambda: os.getenv("GECKO_ACCOUNT_ID", ""))
    monitor_id: str = field(default_factory=lambda: os.getenv("GECKO_MONITOR_ID", ""))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    config_timeout: float = field(default_factory=lambda: float(os.getenv("GECKO_CONFIG_TIMEOUT", "30.0")))
    heat_pump_flow_zone_id: str = field(default_factory=lambda: os.getenv("GECKO_HEAT_PUMP_FLOW_ZONE_ID", "4"))
    heat_pump_default_duration: int = field(default_factory=lambda: int(os.getenv("GECKO_HEAT_PUMP_DEFAULT_DURATION", "30")))
    heat_pump_max_reassert_attempts: int = field(default_factory=lambda: int(os.getenv("GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS", "2")))
    heat_pump_check_interval: float = field(default_factory=lambda: float(os.getenv("GECKO_HEAT_PUMP_CHECK_INTERVAL", "5.0")))
    heat_pump_confirm_timeout: float = field(default_factory=lambda: float(os.getenv("GECKO_HEAT_PUMP_CONFIRM_TIMEOUT", "15.0")))

    # --- Local MQTT ---
    mqtt_host: str = field(default_factory=lambda: os.getenv("MQTT_HOST", "mqtt.example.com"))
    mqtt_port: int = field(default_factory=lambda: int(os.getenv("MQTT_PORT", "1883")))
    mqtt_base_topic: str = field(default_factory=lambda: os.getenv("MQTT_BASE_TOPIC", "gecko"))
    mqtt_username: str = field(default_factory=lambda: os.getenv("MQTT_USERNAME", ""))
    mqtt_password: str = field(default_factory=lambda: os.getenv("MQTT_PASSWORD", ""))
    mqtt_client_id: str = field(default_factory=lambda: os.getenv("MQTT_CLIENT_ID", "gecko-pool-mqtt"))
    mqtt_shutdown_publish_timeout: float = field(default_factory=lambda: float(os.getenv("MQTT_SHUTDOWN_PUBLISH_TIMEOUT", "2.0")))
    #: Trace of all local MQTT messages as JSONL, one file per UTC day. Empty or
    #: "off" disables it.
    mqtt_trace_dir: str = field(default_factory=lambda: os.getenv("MQTT_TRACE_DIR", "/tmp/mqtt"))
    #: 0 keeps all daily files.
    mqtt_trace_retention_days: int = field(default_factory=lambda: int(os.getenv("MQTT_TRACE_RETENTION_DAYS", "7")))

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.oauth_client_id:
            errors.append("GECKO_OAUTH2_CLIENT_ID is required")
        if not self.mqtt_host:
            errors.append("MQTT_HOST is required")
        if self.mqtt_shutdown_publish_timeout < 0:
            errors.append("MQTT_SHUTDOWN_PUBLISH_TIMEOUT must be zero or positive")
        if self.mqtt_trace_dir.strip() and self.mqtt_trace_dir.strip().lower() != "off" and not os.path.isabs(self.mqtt_trace_dir):
            errors.append("MQTT_TRACE_DIR must be an absolute path or 'off'")
        if self.mqtt_trace_retention_days < 0:
            errors.append("MQTT_TRACE_RETENTION_DAYS must be zero or positive")
        if self.heat_pump_default_duration <= 0:
            errors.append("GECKO_HEAT_PUMP_DEFAULT_DURATION must be positive")
        if self.heat_pump_max_reassert_attempts < 0:
            errors.append("GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS must be zero or positive")
        if self.heat_pump_check_interval <= 0:
            errors.append("GECKO_HEAT_PUMP_CHECK_INTERVAL must be positive")
        if self.heat_pump_confirm_timeout <= 0:
            errors.append("GECKO_HEAT_PUMP_CONFIRM_TIMEOUT must be positive")
        elif self.heat_pump_confirm_timeout < self.heat_pump_check_interval:
            errors.append("GECKO_HEAT_PUMP_CONFIRM_TIMEOUT must be at least GECKO_HEAT_PUMP_CHECK_INTERVAL")
        elif self.heat_pump_confirm_timeout < GECKO_MUTATION_BLOCK_SECONDS:
            # A reassert can remain in the blocking call for up to
            # GECKO_MUTATION_BLOCK_SECONDS. If the confirmation window is
            # shorter, the deadline expires during the call and the watchdog
            # reasserts without Gecko having been able to respond.
            errors.append(
                "GECKO_HEAT_PUMP_CONFIRM_TIMEOUT must be at least "
                f"{GECKO_MUTATION_BLOCK_SECONDS:g}s because Gecko zone methods block "
                "up to that long waiting for PUBACK"
            )
        return errors


settings = Settings()
