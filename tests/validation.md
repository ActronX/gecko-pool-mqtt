# Validation

The bridge is tested with `pytest`. The complete requirements and test-case
overview is in [`REQUIREMENTS.md`](../REQUIREMENTS.md). The
`gecko-iot-client` library is **not** tested; all Gecko calls target fakes from
`tests/fakes.py`.

An environment with **Python 3.13** is required because the pinned
`gecko-iot-client` library requires `>=3.13`. The `Dockerfile` already uses
`python:3.13-slim`.

```text
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Syntax check without test dependencies:

```text
python -m compileall -q app tests
```

### Unit Tests

```text
pytest
```

The integration tests are skipped because no broker is running.

### Quick Start on Windows

`run_tests.bat` searches for an interpreter, starts the broker if needed, and
invokes pytest. Without an argument, it searches for a local Python 3.13; if none
is found, the suite runs automatically in Docker, provided Docker is installed.

```text
run_tests.bat
```

| Invocation | Effect |
|---|---|
| `run_tests.bat` | Unit tests, execution mode automatic |
| `run_tests.bat local` | Unit tests with local Python 3.13 |
| `run_tests.bat docker` | Unit tests in the container |
| `run_tests.bat integration` | Unit and integration tests, starts and stops the broker |
| `run_tests.bat all` | same as `integration` |
| `run_tests.bat build` | Installs dependencies or builds the test image |
| `run_tests.bat check` | Displays only the resolved environment |
| `run_tests.bat help` | Help |

Additional arguments are passed unchanged to pytest, for example
`run_tests.bat local -k heat_pump -x`.

A custom interpreter can be specified through `PYTHON`:

```text
set PYTHON=C:\Python313\python.exe
run_tests.bat local
```

Docker mode uses `Dockerfile.test`. It is based on
`python:3.13-slim` and additionally installs `pytest` and `pytest-asyncio`.
The image is rebuilt on every run so that stale state is never
tested; the dependency layer remains cached. The first run therefore
takes longer.

In `integration` mode, the script waits for the broker so that the
integration tests are not skipped, then stops it again afterward.

### Integration Tests

Against a real broker with a fake Gecko client. The Gecko Cloud is
not involved.

```text
docker compose -f docker-compose.test.yml up -d mqtt-test
pytest -m integration
docker compose -f docker-compose.test.yml down
```

The broker host and port can be set via `MQTT_TEST_HOST` and `MQTT_TEST_PORT`.
Without a reachable broker, the integration tests are skipped,
not failed.

### Concurrency Checked Statically

`tests/test_threading.py` uses the AST to verify that no blocking Gecko mutation
occurs outside `asyncio.to_thread`. This is the most important lasting
protection against a regression involving event-loop blocking. A test fails
as soon as a call such as `zone.activate()` is added directly to the controller.
Also protected: local reads such as `zone.active` and
`client.is_connected` remain on the event loop.

### Library Contract Checked

`tests/test_library_contract.py` reads the installed library using
`inspect` and compares signatures, enum members, and the pinned
version with what `app/` expects. This closes the gap caused by
mocks having their own signatures: a change to the library is caught by the
test instead of only at runtime.

The test does not check the library's behavior. The following remain uncovered:
the five-second PUBACK block, the internal reconnect, the state machine of the
real zone objects, and the intermediate state in which the transport is up but
the Vessel is not yet.

### Status

378 unit tests and 9 integration tests pass. The
`gecko-iot-client` library is not tested for its behavior, but it is tested
against the contract that `app/` has with it.

### Manual Smoke Tests

These cases require a real Gecko and are tracked as `TC-MAN-xx` in
[`REQUIREMENTS.md`](../REQUIREMENTS.md).

Observe all topics during the test:

```text
mosquitto_sub -h mqtt.example.com -t "gecko/#" -v
```

If you do not want to run a second client, use the
[MQTT trace](../README.md#mqtt-trace): It contains outgoing and
incoming messages with timestamps and can be fully
analyzed after the test.

Temperature, light, and flow:

| Step | Command | Expected |
|---|---|---|
| Set temperature | `gecko/cmd/temperature/<zone_id>/set` with `{"target_temperature":28.0}` | `result` with `success: true`, then `status/zone/temperature/<zone_id>` shows the target value |
| Light on | `gecko/cmd/lighting/<zone_id>/set` with `{"action":"on"}` | `success: true` and `active: true`; a zone without colour support answers `success: false` |
| Light with colour | `{"action":"on","r":255,"g":120,"b":40}` | `success: true`, colour in `status/zone/lighting/<zone_id>` |
| Light off | `{"action":"off"}` | `success: true`, `active: false` |
| Flow on | `gecko/cmd/flow/<zone_id>/set` with `{"action":"on"}` | `success: true` and `active: true` |
| Flow with `speed` on a non-adjustable pump | `{"action":"on","speed":50}` | `success: false` with `supports on/off only; speed percentage is not supported` |

Heat pump. For a quick run, temporarily set `GECKO_HEAT_PUMP_DEFAULT_DURATION`
to a small value, for example `2`, and keep
`GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS=2`.

| Step | Action | Expected |
|---|---|---|
| Arm | `gecko/cmd/heatPump` with `{"action":"on","duration":2}` | `cmd/heatPump/result` with `success: true`; `status/heatPump/state` changes to `waiting_confirmation` and, after a confirmed zone update, to `running` |
| Extend runtime | again `{"action":"on","duration":10}` | `remaining_seconds` increases noticeably, `state` remains `running`, `armed: true` |
| Do not shorten runtime | `{"action":"on","duration":1}` during a 10-minute request | `remaining_seconds` remains at the longer remaining runtime |
| Watchdog heartbeat | leave `status/heatPump/state` subscribed | Retained payload is updated at least every `GECKO_HEAT_PUMP_CHECK_INTERVAL` seconds, including during `waiting_confirmation` |
| Reassert | stop the pump manually via the Gecko app or a filter cycle | `status/heatPump/reassert` with `reason: zone became inactive`; then back to `running` |
| Expiration | choose `duration` short enough and wait | `status/heatPump/state` with `state: disarmed_expired`, `armed: false`, `remaining_seconds: null`; flow zone is inactive |
| Emergency stop | disconnect the Gecko connection until `GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS` is reached | `status/heatPump/reassert` with `reason: gecko disconnected`, followed once by `status/heatPump/error` with `error: emergency_stop`; **no** `deactivate()` on Gecko. The retained state then shows `state: error` with `armed: false` |
| Re-arm | `status/heatPump/error` was not subscribed: `gecko/status/heatPump/state` shows `state: error` with `armed: false` after the emergency stop; then `{"action":"on"}` | New cycle, `error_count` starts again at `0`, state changes to `waiting_confirmation`. **A restart is not the remedy here:** the process stays up after an emergency stop, and a new process also starts `disarmed`, so only `on` restores the request |
| Off | `{"action":"off"}` | `state: disarmed`, `armed: false`; flow zone is inactive |
| Shutdown | `docker compose stop` | `status/availability` changes retained from `online` to `offline` |
| Crash and restart | `docker compose kill -s KILL gecko-mqtt` | Broker publishes the last will `offline`; the container restarts because of `restart: unless-stopped` and publishes `online` again. The heat-pump request is gone: `status/heatPump/state` shows `disarmed` until the next `{"action":"on"}`. Our side of this is asserted by `TC-OPS-01-01`, `TC-OPS-04-01`, `TC-HP-24-01`, and `TC-HP-24-02`; only the Docker and broker behavior is manual |

Also verify that a deliberately slow blocking Gecko call does not halt the
event loop: during `waiting_confirmation`, watchdog cycle updates and incoming
MQTT commands must continue to run.