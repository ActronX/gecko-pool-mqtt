# Requirements and Test Cases

This document is the normative requirements source for `gecko-pool-mqtt`.
It lists the requirements derived from the code and documentation and their
associated test cases.

- Requirements: `R-<Bereich>-<Nummer>`
- Test Cases: `TC-<Bereich>-<Nummer>`
- Areas: `CFG`, `MQ`, `OA`, `API`, `SER`, `GE`, `CM`, `HP`, `TR`, `THR`, `OPS`

## Scope

The service connects a Gecko in.touch 3 pool controller to a local MQTT broker
and provides temperature, lighting, flow, and heat-pump commands. `README.md`
describes operation; `heatpump_sm.md` describes the heat-pump state machine.

### Out of Scope

The `gecko-iot-client` 1.0.3 library is **not** tested. It is imported and
replaced with fakes from `tests/fakes.py`. Its package metadata, internal state
management, and the Gecko IoT cloud it uses are outside these requirements.
The bridge targets this version's synchronous API generation, not the
asynchronous examples in its package description; see
[Gecko Library and API Version](README.md#gecko-library-and-api-version).

The Gecko IoT cloud itself is not contacted. The only integration test uses a
local broker and a fake Gecko client.

## Prerequisites and Execution

The pinned library requires Python `>=3.13`. The test suite is **not runnable**
in a Python 3.11 environment because runtime dependencies are missing.

```text
python -m pip install -r requirements.txt -r requirements-dev.txt
```

```text
pytest                     # unit tests only; integration tests are skipped
pytest -m integration      # additionally, requires the broker
```

Integration tests against a real broker:

```text
docker compose -f docker-compose.test.yml up -d
pytest -m integration
docker compose -f docker-compose.test.yml down
```

Set the broker host and port through `MQTT_TEST_HOST` and `MQTT_TEST_PORT`.
Without a reachable broker, integration tests are skipped rather than failed.

### Quick Start on Windows

`run_tests.bat` wraps environment discovery, broker management, and the pytest
invocation. It prefers a project-local `.venv`, then `py -3.13`, `py -3`, and
`python`. If it finds no Python 3.13 interpreter, it switches to Docker
automatically. See *Running the Tests* for details.

```text
run_tests.bat
run_tests.bat local -k heat_pump
run_tests.bat integration
run_tests.bat check
```

## Verification Status

| Scope | Result |
|---|---|
| Unit tests | 368 passed |
| Integration tests against mosquitto | 9 passed |
| Gecko IoT cloud | not contacted; all Gecko calls use fakes |
| Library contract | 35 checks against installed 1.0.3 |

## R-CFG Configuration

Quelle: `app/config.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-CFG-01 | Each setting comes from an environment variable with a defined default value. | TC-CFG-01-01, TC-CFG-01-02 |
| R-CFG-02 | `validate()` reports missing `GECKO_OAUTH2_CLIENT_ID` and missing `MQTT_HOST`. | TC-CFG-02-01, TC-CFG-02-02 |
| R-CFG-03 | `validate()` reports non-positive heat-pump intervals and a negative reassert limit. `0` reassert attempts is valid. | TC-CFG-03-01, TC-CFG-05-02 |
| R-CFG-04 | `validate()` reports `HEAT_PUMP_CONFIRM_TIMEOUT < HEAT_PUMP_CHECK_INTERVAL` and also reports when `HEAT_PUMP_CONFIRM_TIMEOUT` is less than the library's five-second blocking PUBACK wait. | TC-CFG-04-01 bis TC-CFG-04-03 |
| R-CFG-05 | A valid configuration produces an empty error list. | TC-CFG-05-01 |
| R-CFG-06 | `validate()` reports negative `MQTT_SHUTDOWN_PUBLISH_TIMEOUT`; `0` is allowed and disables waiting. | TC-CFG-06-01, TC-CFG-06-02 |
| R-CFG-08 | `MQTT_TRACE_DIR` and `MQTT_TRACE_RETENTION_DAYS` come from the environment; the defaults are `/tmp/mqtt` and `7`. Empty or `off` disables the trace, `0` means unlimited retention and is valid. A relative path and negative retention are reported. | TC-CFG-08-01 bis TC-CFG-08-05 |

## R-MQ Bridge, Topics, and Publishing

Quelle: `app/mqtt_bridge.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-MQ-01 | `topic()` uses `MQTT_BASE_TOPIC` as a prefix and normalizes leading and trailing slashes. | TC-MQ-01-01, TC-MQ-01-02, TC-MQ-01-03 |
| R-MQ-02 | The client uses `MQTT_CLIENT_ID`, sets credentials only when configured, sets the Last Will to retained QoS 1 `status/availability=offline`, and limits reconnect backoff to 1 through 60 seconds. | TC-MQ-02-01 bis TC-MQ-02-06, TC-INT-06-03 |
| R-MQ-03 | After connecting, the client subscribes with QoS 1 to `cmd/+/+/set`, `cmd/heatPump`, `auth/response`, and `auth/login`. | TC-MQ-03-01, TC-INT-01-01 |
| R-MQ-04 | After connecting, the client publishes retained `status/availability=online`. A rejected connection does not publish `online`. | TC-MQ-04-01, TC-MQ-04-02, TC-INT-01-01, TC-INT-01-02 |
| R-MQ-05 | Dispatch occurs only for exactly three segments `<type>/<zone_id>/set` below `cmd/`. An empty payload produces an empty object and then fails validation. The auth topics reach their own callbacks. | TC-MQ-05-01 bis TC-MQ-05-05 |
| R-MQ-06 | `temperature` requires a numeric `target_temperature`. | TC-MQ-06-01 |
| R-MQ-07 | `lighting` requires `action` in `{on,off}`, rejects `effect` together with `off`, and requires `r`, `g`, `b`, and `intensity` as integers from 0 through 255. `bool` is currently **not** rejected; see *Known Gaps*. | TC-MQ-07-01 bis TC-MQ-07-05 |
| R-MQ-08 | `flow` requires `action` in `{on,off}` and a numeric `speed`; `bool` is rejected. | TC-MQ-08-01, TC-MQ-08-02 |
| R-MQ-09 | An unknown `command_type` produces an error result. | TC-MQ-09-01 |
| R-MQ-10 | `heatPump` requires `action` in `{on,off}`; `duration` must be numeric, integral, and greater than zero. | TC-MQ-10-01, TC-MQ-10-02 |
| R-MQ-11 | Invalid payloads produce an error result on the matching result topic and do not stop the service. Command acknowledgements and the `reassert` and `error` events are not retained; the challenge is deleted as an empty retained message. | TC-MQ-11-01, TC-MQ-16-01 bis TC-MQ-16-03, TC-INT-03-01 |
| R-MQ-12 | `publish()` returns `MQTTMessageInfo`; when `rc != SUCCESS`, it returns `None` and logs a warning. Dictionaries are serialized compactly, and strings are sent unchanged. | TC-MQ-12-01 bis TC-MQ-12-04 |
| R-MQ-13 | `stop()` confirms retained `offline` before `disconnect()`. | TC-MQ-13-01, TC-INT-05-01 |
| R-MQ-14 | `stop()` calls `disconnect()` and `loop_stop()` even after a timeout, publish exception, or failed publish. | TC-MQ-14-01 bis TC-MQ-14-03 |
| R-MQ-17 | Every message sent through `publish()` and every message received through `_on_message` is stored as one JSON line in the trace, with outgoing lines also carrying `qos` and `retain`. The line is written before the message is dispatched. A failed attempt sent to the broker is also recorded. | TC-MQ-17-01 bis TC-MQ-17-05, TC-TR-01-01 bis TC-TR-01-05 |

## R-TR Trace

Quelle: `app/mqtt_trace.py`, verdrahtet in `app/mqtt_bridge.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-TR-01 | Each message occupies exactly one JSON line with `ts` (UTC with microseconds), `dir` (`in` or `out`), and `topic`. Outgoing lines also carry `qos` and `retain`; incoming lines do not. The payload is preserved unchanged, including when empty or not valid JSON. Line order matches call order. | TC-TR-01-01 bis TC-TR-01-05 |
| R-TR-02 | One file `mqtt-YYYY-MM-DD.jsonl` is written per UTC day. Rotation also occurs when the process runs across midnight; the time comes from a clock injection point. `close()` closes the file, repeated closes are allowed, and a later call writes to the daily file again. | TC-TR-01-06, TC-TR-01-07, TC-TR-01-17, TC-TR-01-18 |
| R-TR-03 | `MQTT_TRACE_RETENTION_DAYS` deletes older daily files on creation and at every day change. `0` retains everything. File names that do not match the pattern remain untouched. | TC-TR-01-08 bis TC-TR-01-10 |
| R-TR-04 | Empty, `off`, and `None` disable the trace without creating a directory. | TC-TR-01-11, TC-TR-01-12, TC-MQ-17-05 |
| R-TR-05 | The payload of `auth/response` is replaced with `<redacted>` and only its length is recorded as `bytes`. All other topics remain in plaintext, and the message continues unchanged. | TC-TR-01-13, TC-TR-01-14, TC-MQ-17-04 |
| R-TR-06 | `record()` never exposes an exception. Filesystem errors generate one warning only and are not reported again. The MQTT path continues unchanged. | TC-TR-01-15, TC-TR-01-16 |
| R-TR-07 | The write operation is protected against concurrency between the paho thread and event loop; parallel calls produce only intact lines. The target directory is created on initialization. | TC-TR-01-19, TC-TR-01-20 |
| R-TR-08 | Every line can be read from the file without `close()`; writes use `flush()` but not `fsync`. | TC-MQ-17-01 |

## R-OA OAuth2 PKCE

Quelle: `app/oauth_flow.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-OA-01 | PKCE generates a random verifier and its S256 challenge, base64url without padding. | TC-OA-01-01 bis TC-OA-01-03 |
| R-OA-02 | `build_authorize_url()` includes `client_id`, `redirect_uri`, `response_type`, `scope`, `audience`, `code_challenge`, `code_challenge_method`, and `state`, and stores the verifier and state. | TC-OA-02-01, TC-OA-02-02 |
| R-OA-03 | `exchange_code` without a stored verifier raises `RuntimeError`. | TC-OA-03-01 |
| R-OA-04 | A different `state` is rejected, and the matching one is accepted. | TC-OA-04-01, TC-OA-04-02 |
| R-OA-05 | Status 401 or 403 during token exchange produces `OAuthAuthenticationError` with the status; other errors produce `RuntimeError`. | TC-OA-05-01, TC-OA-05-02 |
| R-OA-06 | A response without `access_token` produces `OAuthAuthenticationError`. | TC-OA-06-01 |
| R-OA-07 | A successful exchange persists tokens atomically through a temporary file and clears the PKCE state. | TC-OA-07-01, TC-OA-07-02 |
| R-OA-08 | A still-valid access token is returned without refresh with a 60-second safety margin. | TC-OA-08-01, TC-OA-08-02 |
| R-OA-09 | An expired token triggers the refresh grant; 401, 403, or `invalid_grant` produce `OAuthAuthenticationError`, and server errors produce `RuntimeError`. If the response lacks `refresh_token`, the old one is retained. | TC-OA-09-01 bis TC-OA-09-03 |
| R-OA-10 | Without a refresh token, `get_valid_access_token()` raises `RuntimeError`. | TC-OA-10-01 |
| R-OA-11 | Provider error text is truncated to 240 characters and exposes only `error_description` or `error`. Unknown content is not emitted unfiltered. | TC-OA-11-01 bis TC-OA-11-06 |
| R-OA-12 | Persisted tokens survive a restart. A file without `refresh_token` is unusable. `clear_tokens()` removes it. | TC-OA-12-01 bis TC-OA-12-04 |
| R-OA-13 | Concurrent token requests are serialized by a lock; exactly one refresh is triggered. | TC-OA-13-01 |

## R-API Gecko REST API

Quelle: `app/gecko_api.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-API-01 | The user identifier comes from Auth0 `userinfo`. 401 and 403 produce `OAuthAuthenticationError`, and a missing `sub` produces `ValueError`. | TC-API-01-01 bis TC-API-01-03 |
| R-API-02 | Account discovery returns `accountId` and the name; without `accountId`, it raises `ValueError`. 401 and 403 from the Gecko API are translated, while other HTTP errors remain unchanged. | TC-API-02-01 bis TC-API-02-04 |
| R-API-03 | `async_get_vessels` accepts a list and dictionaries with `vessels`, `data`, or `results`; other forms produce an empty list. | TC-API-03-01 bis TC-API-03-03 |
| R-API-04 | The broker URL comes from `brokerUrl` in the livestream response. If the value is missing or the response is unexpected, `RuntimeError` names the available keys. | TC-API-04-01 bis TC-API-04-03 |
| R-API-05 | The synchronous refresh callback uses `run_coroutine_threadsafe` and returns the new broker URL. On auth errors it returns `None` and reports the error; other errors are swallowed. | TC-API-05-01 bis TC-API-05-03 |

## R-SER Zone Serialization

Quelle: `app/zone_serializer.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-SER-01 | Temperature zones provide `current_temperature`, `target_temperature`, `status`, `eco_mode`, `min_set_point`, and `max_set_point`. Missing status and mode produce `null`. Each zone type has an English label. | TC-SER-01-01 bis TC-SER-01-04 |
| R-SER-02 | Lighting zones provide `active`, `color`, and `effect`; without a set color, `color` is `null`. | TC-SER-02-01, TC-SER-02-02 |
| R-SER-03 | Flow zones provide `active`, `speed`, `initiators`, `initiator_labels`, `capabilities`, `supports_speed_percentage`, `supports_turn_on`, `supports_turn_off`, `speed_config`, and `presets`. | TC-SER-03-01 bis TC-SER-03-03 |
| R-SER-04 | Initiator codes remain unchanged. Unknown codes receive the label `unknown:<code>`. All codes documented in the README are covered. | TC-SER-04-01 bis TC-SER-04-03 |
| R-SER-05 | `supports_speed_percentage` is true only when `speed_config.stepIncrement` is nonzero. | TC-SER-05-01, TC-SER-05-02 |
| R-SER-06 | Zones are grouped by the labels `temperature`, `lighting`, and `flow`. Capabilities are returned as a sorted value list. Unknown types use their value. | TC-SER-06-01 bis TC-SER-06-03 |
| R-SER-07 | An unknown zone receives an empty `state`. | TC-SER-07-01 |
| R-SER-08 | Connectivity is represented through `to_dict()`; objects without this method are returned as `{"raw": ...}`. | TC-SER-08-01 bis TC-SER-08-03 |

## R-GE Connection and Lifecycle

Quelle: `app/pool_controller.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-GE-01 | `connect()` runs in a daemon thread, not on the event loop. The monitor ID comes from vessel discovery. | TC-GE-01-01, TC-GE-01-02 |
| R-GE-02 | Connect errors use exponential backoff from 5 to 10 to 20 seconds, the client is disconnected between attempts, and `authenticated` remains `false`. A stale generation ends the loop immediately. | TC-GE-02-01, TC-GE-02-02 |
| R-GE-03 | A successful connect publishes `auth_status: authenticated`. | TC-GE-03-01 |
| R-GE-04 | A stale connection generation must not set `authenticated`. | TC-GE-04-01, TC-GE-04-02 |
| R-GE-05 | Without persisted tokens, `login_required` is published with a challenge and no client is started. | TC-GE-05-01 |
| R-GE-06 | An OAuth error during autostart produces `reauth_required` and a new challenge. | TC-GE-06-01 |
| R-GE-07 | `_mark_reauth_required` sets `reauth_required`, increments the generation exactly once, disconnects the client, and publishes connectivity with `auth_status` and `reauth_required`. Repeated triggers make no further changes. | TC-GE-07-01, TC-GE-07-02 |
| R-GE-08 | Without a discoverable monitor ID or without vessels, startup aborts with `RuntimeError`. | TC-GE-08-01, TC-GE-08-02 |
| R-GE-09 | `_wait_until_connected` raises immediately when the client is missing instead of reporting success. | TC-GE-09-01 |
| R-GE-10 | `_wait_until_connected` waits at most `settings.config_timeout` and then raises with a clear message. | TC-GE-10-01, TC-GE-10-02 |
| R-GE-11 | The bridge retry loop applies **only to the initial connection**. After the first successful `connect()`, `_connect_worker` returns and is not started again. The bridge does not handle a later connection loss. | TC-GE-11-01 |
| R-GE-12 | The library alone handles an operational outage. The awscrt lifecycle callback triggers a reconnect chain in the `gecko-iot-client` 1.0.3 transport. The bridge must provide a `token_refresh_callback`; otherwise the transport schedules no reconnect. | TC-GE-12-01 |

## R-CM Commands

Quellen: `app/pool_controller.py`, `app/mqtt_bridge.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-CM-01 | Missing authentication or a missing client produces `success: false` and no Gecko mutation. | TC-CM-01-01, TC-CM-01-02 |
| R-CM-02 | `temperature` calls `set_target_temperature`. | TC-CM-02-01, TC-INT-02-02 |
| R-CM-03 | `lighting` calls `deactivate`, `set_effect`, or `set_color` depending on the payload. An effect takes precedence over the color. | TC-CM-03-01 bis TC-CM-03-03 |
| R-CM-04 | `flow` with `speed` on a pump that cannot be regulated yields `success: false` with `Flow zone <id> supports on/off only; speed percentage is not supported`. | TC-CM-04-01 |
| R-CM-05 | `flow` calls `activate` without `speed`, `set_speed` with `speed`, and `deactivate` for `off`. | TC-CM-05-01 bis TC-CM-05-03, TC-INT-02-01 |
| R-CM-06 | An unknown `command_type` produces `success: false`. | TC-CM-06-01 |
| R-CM-07 | Exceptions from the Gecko call and unknown zones produce an error result with the error text. | TC-CM-07-01, TC-CM-07-02 |
| R-CM-08 | `_extract_callback_values` reads code and state from `code` or the complete `redirect_url`. If the code is wrapped in a `redirect` URL parameter, that parameter is evaluated. Without a code, an error is raised. | TC-CM-08-01 bis TC-CM-08-03 |
| R-CM-09 | A successful result contains `success: true`, a message, and `zone_id`. | TC-INT-02-01 |

## R-HP Heat-Pump State Machine

Quellen: `app/heat_pump_controller.py`, normativ beschrieben in
[`heatpump_sm.md`](heatpump_sm.md).

| ID | Requirement | Test Cases |
|---|---|---|
| R-HP-01 | `on` activates an inactive zone, sets `armed`, starts the watchdog, waits for connection readiness, and reports `waiting_confirmation`. If Gecko confirms during blocking `activate()`, it reports `running` instead. The result field `state` reports the state produced by the command, not the previous state. Without `duration`, `GECKO_HEAT_PUMP_DEFAULT_DURATION` applies. An initial `activate` error disarms, reports `success: false`, and stops the watchdog. | TC-HP-01-01 bis TC-HP-01-16, TC-INT-04-01 |
| R-HP-02 | `on` does not activate an already active zone and reports `running`. `off` remains possible afterward. | TC-HP-02-01 bis TC-HP-02-03 |
| R-HP-03 | Pending confirmation and the deadline are set **before** the blocking call starts. An `active: false` update while waiting does not trigger a second reassert. `_observe_zone_update` sets Pending synchronously so the watchdog cannot start a second reassert in the same window. | TC-HP-03-01, TC-HP-03-02, TC-HP-06-01, TC-HP-06-02 |
| R-HP-04 | A repeated `on` extends runtime to the maximum and never shortens it. An active zone is not activated again. | TC-HP-04-01 bis TC-HP-04-03 |
| R-HP-05 | A zone update with `active: true` confirms immediately, clears Pending and the deadline, and resets `error_count` without waiting for the next watchdog cycle. `running` is published only when this causes a state change. A repeated `active: true` while already `running` publishes nothing because Gecko sends updates for all zones and often sends duplicates in quick succession. | TC-HP-05-01 bis TC-HP-05-04 |
| R-HP-06 | A zone update with `active: false` triggers exactly one reassert. While Pending is set, no further update reasserts. A failed reassert counts but does not immediately trigger the emergency stop. If Gecko confirms during the reassert's blocking `activate()`, `running`, not `waiting_confirmation`, is reported. | TC-HP-06-01 bis TC-HP-06-04 |
| R-HP-07 | No activate reassert occurs for a disconnected client. The watchdog counts the outage as an error; see R-HP-23. An update without a matching zone is ignored. | TC-HP-07-01, TC-HP-07-02 |
| R-HP-08 | Without confirmation within the timeout, `error_count` increases with `activation not confirmed within confirm_timeout`, and activation is then retried. | TC-HP-08-01, TC-HP-08-02 |
| R-HP-09 | The watchdog reports an inactive zone with `zone became inactive`. | TC-HP-09-01 |
| R-HP-10 | A reassert error is reported with `activate_called: false` and its text in `activate_error`. `attempt` equals the error count and is `0` before the first error. `confirmed` is `true` only when Gecko confirms activation during blocking `activate()`; confirmation through a zone update reports `confirmed: false`. | TC-HP-10-01, TC-HP-06-01, TC-HP-06-04 |
| R-HP-11 | When the limit is reached, `emergency_stop` is published, the controller is disarmed, and state `error` is reported. This applies to every trigger, including the offline path. No `deactivate()` is sent. | TC-HP-11-01, TC-HP-11-02, TC-HP-23-06 |
| R-HP-12 | With `GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS=0`, no emergency stop occurs. | TC-HP-12-01 |
| R-HP-13 | `off` deactivates the zone, sets `armed: false`, clears the deadline, and reports `disarmed` and `Heat-pump pump request cancelled`. | TC-HP-13-01, TC-HP-13-02 |
| R-HP-14 | An error or missing client during `off` is logged, and the result remains successful. | TC-HP-14-01, TC-HP-14-02 |
| R-HP-15 | An unknown action produces `success: false`; the result names the configured zone. | TC-HP-15-01, TC-HP-15-02 |
| R-HP-16 | On expiry, the controller deactivates the zone best effort and reports `disarmed_expired`, `armed: false`, and `remaining_seconds: null`. | TC-HP-16-01 |
| R-HP-17 | Every command increments the generation. A late return from the blocking call must not report success or rearm the controller after `off`, expiry, emergency stop, shutdown, or a newer command. After `off` and shutdown, no zone update triggers another reassert. | TC-HP-17-01 bis TC-HP-17-14 |
| R-HP-18 | `stop()` disarms, stops the watchdog, and reports `disarmed`. | TC-HP-18-01 |
| R-HP-19 | `error_count` increases with every error. It resets on `off`, on a new `on` from the disarmed state, on watchdog or zone-update confirmation, and on `stop()`. It does **not** reset on expiry or emergency stop; payloads `disarmed_expired` and `error` therefore retain the request's error count. Disconnected connections and zone lookup errors count as errors; see R-HP-23. | TC-HP-19-01 bis TC-HP-19-08 |
| R-HP-20 | Missing authentication for `on` produces `success: false` without activation. | TC-HP-20-01 |
| R-HP-21 | The watchdog publishes the retained state exactly once per cycle with `armed`, `max_errors`, and `remaining_seconds`. A cycle that publishes the state itself suppresses the end-of-cycle publication. Without a Gecko connection, it publishes `disconnected`. The published `state` never contradicts `armed` or `remaining_seconds`: `disarmed` appears only with `armed: false` and `remaining_seconds: null`; `waiting_confirmation`, `running`, and `disconnected` appear only with `armed: true` and remaining runtime. While `activate()` blocks and confirmation has not arrived, the watchdog publishes `waiting_confirmation`, not the previous command's state name. | TC-HP-21-01 bis TC-HP-21-05, TC-HP-01-16 |
| R-HP-22 | Reassert and error payloads carry the current initiator codes. | TC-HP-22-01 bis TC-HP-22-03 |
| R-HP-23 | If the Gecko IoT cloud connection is disconnected or the client is missing, the watchdog treats this as an error: it increments the error count, publishes a reassert event with `reason: gecko disconnected` and `activate_called: false`, and reports `disconnected`. It counts at most once per `GECKO_HEAT_PUMP_CHECK_INTERVAL`, in practice once per offline cycle, while `disconnected` appears every cycle. The zone is **not** activated and `armed` remains `true`. These errors count toward `GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS` and can trigger the emergency stop; with `0`, no emergency stop occurs. Only the watchdog counts, not the zone update, which exits early when disconnected. Reconnecting alone does **not** reset the count; confirmation, `off`, or a new `on` does. | TC-HP-23-01 bis TC-HP-23-10 |

### Limits of the Activity Check

The controller evaluates only `zone.active` and does **not** inspect the
initiator. Activity from `FI` or `CD` therefore also counts as confirmation, and
`running` does not guarantee that `UD` or `HTP` is currently set. This
simplification is intentional and documented in `heatpump_sm.md` under
*Limits of the Activity Check*.

## R-THR Concurrency

Quellen: alle Module in `app/`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-THR-01 | No blocking Gecko mutation runs directly on the event loop. `activate`, `deactivate`, `set_speed`, `set_color`, `set_effect`, and `set_target_temperature` appear only as arguments to `asyncio.to_thread` or in the three helpers `set_target_temperature`, `set_light`, and `set_flow`. `run_in_executor` is not used. Pure reads remain on the event loop. | TC-THR-01-01 bis TC-THR-01-04 |
| R-THR-02 | MQTT commands are moved to the event loop through `run_coroutine_threadsafe`. | TC-THR-05-01 |
| R-THR-03 | Gecko zone updates are moved to the event loop through `call_soon_threadsafe` and receive the same zone instance as the controller. | TC-THR-06-01, TC-THR-06-02 |
| R-THR-04 | A slow blocking Gecko call does not stop the event loop; the watchdog continues publishing. | TC-THR-02-01, TC-MAN-01 |
| R-THR-05 | A cancelled task produces no unhandled task exception. | TC-THR-03-01 |
| R-THR-06 | The reassert task started by `_observe_zone_update` handles all exceptions internally. | TC-THR-04-01 |
| R-THR-07 | The standard executor limits concurrency for blocking calls. Calls run concurrently up to its thread limit; additional calls queue and add their wait time. The event loop remains responsive in both cases. | TC-THR-07-01 bis TC-THR-07-03 |
| R-THR-08 | The trace is fed from the paho thread and the event-loop thread. A lock serializes writes and produces only intact lines even for parallel calls. | TC-TR-01-19 |

TC-THR-01 is static and permanently protects the central concurrency
requirement without Gecko or a broker.

## R-OPS Operation

Quellen: `app/main.py`, `docker-compose.yml`, `app/config.py`.

| ID | Requirement | Test Cases |
|---|---|---|
| R-OPS-01 | SIGINT and SIGTERM trigger an orderly shutdown. | TC-MAN-02 |
| R-OPS-02 | During shutdown, `controller.stop()` is called first, followed by `bridge.stop()`. | TC-OPS-02-01 |
| R-OPS-03 | Retained `offline` is confirmed before `disconnect()` and is then readable by new subscribers. | TC-INT-05-01, TC-INT-06-01, TC-INT-06-02 |
| R-OPS-04 | `restart: unless-stopped` restarts the container after an error. | TC-MAN-02 |
| R-OPS-05 | Configuration errors are advisory: `settings.validate()` is logged, but the service starts anyway. | TC-CFG-04-01, TC-MAN-06 |

## Manual Test Cases

These cases require a real Gecko, real PUBACK behavior, or a broker connection
and cannot be automated. The procedure is described in the README section
*Manual Smoke Tests*.

| ID | Requirement | Verification |
|---|---|---|
| TC-MAN-01 | R-THR-04 | A real five-second PUBACK block; the event loop remains responsive and the watchdog continues publishing. |
| TC-MAN-02 | R-OPS-01, R-OPS-04 | `docker compose stop` performs an orderly shutdown with retained `offline`; a crash triggers a restart. |
| TC-MAN-03 | R-HP-09 | A real Gecko initiator change stops the zone and triggers the reassert. |
| TC-MAN-04 | R-GE-02, R-HP-12 | Watchdog behavior during a prolonged Gecko outage until the emergency stop. |
| TC-MAN-05 | R-OA-09 | A real refresh after an access token expires. |
| TC-MAN-06 | R-OPS-05 | Intentionally invalid configuration; the service starts with `Configuration errors: ...` and continues running. |

## Test Structure

| File | Area |
|---|---|
| `tests/fakes.py` | Fakes for zones, Gecko client, paho client, aiohttp session, and bridge |
| `tests/conftest.py` | Settings isolation, broker discovery, and controller harnesses |
| `tests/test_config.py` | R-CFG |
| `tests/test_mqtt_bridge.py` | R-MQ |
| `tests/test_oauth_flow.py` | R-OA |
| `tests/test_gecko_api.py` | R-API |
| `tests/test_zone_serializer.py` | R-SER |
| `tests/test_pool_controller.py` | R-GE, R-CM, R-OPS-02 |
| `tests/test_library_contract.py` | Contract between `app/` and the installed library |
| `tests/test_heat_pump_controller.py` | R-HP |
| `tests/test_mqtt_trace.py` | R-TR |
| `tests/test_threading.py` | R-THR |
| `tests/integration/test_mqtt_roundtrip.py` | TC-INT against a real broker |

The fakes reproduce only the signatures that `app/` actually uses. They are not
a replacement for the library and are not checked for correctness themselves.

### Limits of the Fakes

`tests/fakes.py` has **independent** signatures. If the library changed a call
signature, the suite would remain green and the error would appear only at
runtime.
`tests/test_library_contract.py` closes this gap: the test reads the real library
with `inspect` and compares signatures, enum members, and the pinned version.
It does **not** test library behavior; it checks only whether the assumptions in
`app/` still hold.

Not reproduced and therefore not covered: the five-second PUBACK block, the
internal reconnect, the desired-state state machine of real zone objects, and
the intermediate `is_connected` state in which the transport is connected but
the vessel is not yet connected.

### Configuration Isolation

The repository contains a real `.env`, which flows into the settings singleton
when `app.config` is imported. The automatically enabled `isolated_settings`
fixture overrides every setting touched by tests and places `OAUTH_TOKEN_FILE`
in a temporary directory. Without this fixture, tests would write to
`/data/tokens.json`.

### Handling Time

Heat-pump tests set `heat_pump_check_interval` and
`heat_pump_confirm_timeout` to small values. Expiry is triggered through
`duration: 0`, not by real waiting. Confirmation expiry is simulated through
the zone fake with `block()` and `delay()`, not through the library's five
seconds.

## Known Gaps

- **`gecko/auth/status` does not reflect an outage.** `authenticated` is set only
  by `_mark_connected`, which is called only by `_connect_worker`. Because this
  thread ends after the initial connection, the value remains `authenticated`
  during an outage even though the connection is gone. If the connection
  recovers by itself, the auth status remains unchanged. Use
  `gecko/status/connectivity` as the connection indicator. See R-GE-11 and R-GE-12.
- **`bool` is accepted for lighting colors.** The check for `r`, `g`, `b`, and
  `intensity` in `_validate_command` does not reject `bool`, so `true` is taken as
  `1`. For `flow`, `bool` is explicitly rejected for `speed`. The checks are
  therefore inconsistent. TC-MQ-07-05 records the current behavior so a change
  is deliberate. Recommendation: reject `isinstance(value, bool)` for color
  channels as for `speed`.
- **`error_count` remains after expiry and emergency stop.** See R-HP-19. This
  is functionally harmless because the next `on` starts a new count, but it can
  be confusing in `status/heatPump/state`.
- **`attempt` starts at 0.** The first reassert publishes `attempt: 0` because
  the value equals the error count. It is not a sequence number.
- R-GE-03 is checked only through the success path. Publishing a snapshot after
  a successful connect is not tested separately.
- Shutdown ordering is checked statically because a real SIGTERM cannot trigger
  `loop.add_signal_handler` on Windows. TC-MAN-02 covers the real flow.
- The integration test replaces the Gecko client with a fake. Library behavior
  during a real PUBACK is therefore not covered.

## Running the Tests

Python 3.13 or newer is required. If no local interpreter is available, the
suite can run in Docker.

```text
run_tests.bat                 Unit tests, execution mode automatic
run_tests.bat local           Unit tests with local Python
run_tests.bat docker          Unit tests in the container
run_tests.bat integration     Unit and integration tests with the test broker
run_tests.bat build           Installs dependencies or builds the image
run_tests.bat check           Shows the resolved environment
```

Without `run_tests.bat`:

```text
python -m pip install -r requirements.txt -r requirements-dev.txt
pytest
docker compose -f docker-compose.test.yml up -d mqtt-test
pytest -m integration
```

`Dockerfile.test` builds the test image on `python:3.13-slim` and additionally
installs `pytest` and `pytest-asyncio`. The service container from `Dockerfile`
intentionally contains no test dependencies.
