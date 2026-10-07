# Development

## Dependencies

`requirements.txt` pins four direct dependencies:

| Package | Version | Reason |
|---|---|---|
| `gecko-iot-client` | `1.0.3` | Gecko Cloud integration; only API dependency |
| `awscrt` | `0.36.1` | Runtime dependency of `gecko-iot-client` 1.0.3 |
| `awsiotsdk` | `1.31.0` | Runtime dependency of `gecko-iot-client` 1.0.3 |
| `aiohttp` | `>=3.9.0` | HTTP client for OAuth and Gecko REST API |
| `paho-mqtt` | `>=2.1.0` | Local MQTT broker |
| `python-dotenv` | `>=1.0.0` | Loads `.env` |

Regarding `awscrt` and `awsiotsdk`: The bridge creates a
`MqttTransporter(broker_url=...)` with a WebSocket URL and embedded JWT
and does **not** use the certificate-based AWS IoT transport from the library's
example. The two AWS packages are nevertheless required because
`gecko-iot-client` 1.0.3 declares them as runtime dependencies in its package
metadata (`requires_dist`):

```text
awscrt>=0.19.0
awsiotsdk>=1.28.1
```

Removing them from `requirements.txt` would leave the environment declared by
the library and only makes sense once an upstream release no longer treats them
as mandatory dependencies. The versions pinned here meet the minimum
requirements and keep resolution reproducible. To prevent the gecko version
from changing accidentally, `gecko-iot-client` should remain pinned to exactly
1.0.3 and be checked against the API list under
[Gecko library and API version](#gecko-library-and-api-version) before any
change.

### Gecko Library and API Version

`requirements.txt` pins `gecko-iot-client==1.0.3`. This is the latest version
published on PyPI (published 2026-06-02), so the pin is current. The package
requires Python `>=3.13`; the `Dockerfile` therefore uses `python:3.13-slim`.

However, the published package description is **not** reliable and differs from
the shipped 1.0.3 code:

- The PyPI README describes the library as an "asynchronous Python client
  library" and shows `await light.activate()` and
  `MqttTransporter(endpoint=..., device_id=...)`.
- The bridge deliberately uses the **synchronous** API generation in 1.0.3:
  `MqttTransporter(broker_url=..., monitor_id=..., token_refresh_callback=...)`
  and blocking zone methods.

This bridge targets the 1.0.3 implementation, not the examples in the PyPI
README. API surface used:

| Area | API used in 1.0.3 |
|---|---|
| Connection | `GeckoIotClient(idd=..., transporter=..., config_timeout=...)`, blocking `connect()` / `disconnect()` |
| Zone lookup | `get_zone_by_id_and_type(ZoneType..., zone_id)`, `get_zones()` |
| Zone mutation | `FlowZone.activate()` / `deactivate()` / `set_speed()`, `LightingZone.set_color()` / `set_effect()`, `TemperatureControlZone.set_target_temperature()` |
| Events | `client.on(EventChannel.ZONE_UPDATE / CONNECTIVITY_UPDATE / OPERATION_MODE_UPDATE, cb)` |

Important behavior in this API generation: the zone methods
`activate()`, `deactivate()`, `set_speed()`, `set_color()`, `set_effect()` and
`set_target_temperature()` are **blocking**. They wait internally for up to five
seconds for the broker's PUBACK confirmation. Do not call them directly on the
asyncio event loop.

The bridge therefore calls each mutation through `await asyncio.to_thread(...)`.
The call blocks a worker thread instead of the event loop; MQTT, the watchdog,
and status publishes continue in the meantime. Read-only accesses such as
`client.is_connected`, `zone.active`, `zone.initiators` and
`get_zone_by_id_and_type()` remain on the event loop.

The published project documentation is available at
<https://geckoal.github.io/gecko-iot-client/>. Check it against the 1.0.3 API
listed above before using code examples from it.

Two additional packages are used for testing; they are listed in `requirements-dev.txt`
and do not belong in the runtime image: `pytest` and `pytest-asyncio`. They are
installed into a separate test image via `Dockerfile.test`.

## Local Development

Install dependencies and start the application from the project
directory:

```text
python -m pip install -r requirements.txt
python -m app.main
```

OAuth tokens are stored locally at the path configured in `OAUTH_TOKEN_FILE`.
For local execution, this path should point to an existing directory, for
example `./tokens.json`.

### Thread Boundaries

The Gecko client's callbacks originate from background threads. Status publishes
from the local Paho client are thread-safe; OAuth and command operations are
delegated back to the asyncio event loop.

To prevent the blocking Gecko zone methods from halting the event loop, the
following structure applies:

```text
paho-MQTT thread ──run_coroutine_threadsafe──▶ asyncio event loop
Gecko callback thread ──call_soon_threadsafe──▶ asyncio event loop
asyncio event loop ──await asyncio.to_thread──▶ worker thread ──▶ Gecko (blocking)
```

- Incoming MQTT commands and Gecko zone updates are marshalled onto the event
  loop; all state changes take place there.
- Each blocking zone mutation runs in a worker thread of the default executor.
  A Gecko call waits internally for PUBACK for up to five seconds; the event
  loop remains responsive.
- A Gecko call can outlive an already cancelled controller task. Late returns
  are ignored if the request has since ended, expired, been stopped via
  emergency stop, or the process has shut down.
- An in-progress `GeckoIotClient.connect()` is not forcibly aborted during
  shutdown; the connection is established in a daemon thread and terminated
  separately.

### Limits with Many Blocking Calls

`asyncio.to_thread` uses the loop's default executor. It has
`min(32, cpu_count + 4)` threads and is shared by both controllers. In
a container without a CPU limit, this can be considerably fewer; `python:3.13-slim`
without a `cpus` setting is the default.

Two practical limits follow, documented by
`tests/test_threading.py`:

- **Blocking calls run simultaneously up to this thread limit.** A burst that
  switches multiple zones therefore does not block the overall operation.
- **Every call beyond that limit is queued, and the wait time accumulates.** With
  five concurrent commands and three free threads, the last command may wait
  for a multiple of 5 seconds.

The event loop remains responsive in both cases, and the watchdog
continues to publish. Anyone issuing many commands in one batch should
account for the response time. The wait for confirmation of a single
zone is normally well below the five-second upper bound because the
broker responds quickly.

## Connection and Reconnect

Two completely different mechanisms must be distinguished.

### Initial Connection: Bridge Retry Loop

`PoolController._connect_worker` calls `client.connect()` in a daemon thread
and retries on failure with exponential backoff: 5 s, 10 s, 20 s, up to a
maximum of 300 s (`GECKO_CONFIG_TIMEOUT` additionally limits each individual
attempt). The loop continues indefinitely.

This applies **only to the initial connection**. Once `connect()` has succeeded
once, the thread returns and is not restarted.

### Runtime Failure: Library Reconnect and Bridge Recovery

The library detects a later connection loss through an awscrt lifecycle callback
and first attempts reconnecting automatically:

```text
on_lifecycle_disconnection
  → MqttTransporter._handle_connection_lost
  → _schedule_reconnect (daemon thread, exponential backoff)
      on failure: retry
```

According to the 1.0.3 implementation, the wait sequence is **1 s, 2 s, 4 s,
8 s, 16 s** for a total of 5 attempts. It does not give up afterward: the
counter is reset and a cooldown with a forced token refresh is started. If the
token has already expired when the disconnect occurs, the token is renewed
before reconnecting anyway so that the new broker URL is valid.

This requires a configured `token_refresh_callback`, which the bridge supplies.

The bridge additionally treats an incomplete `CONNECTIVITY_UPDATE` as a
fallback signal. It schedules exactly one recovery task on the asyncio loop and
waits `GECKO_RECOVERY_DELAY` seconds, **120 seconds by default**, before doing
anything. This delay deliberately gives the library reconnect chain time to
self-heal. The bridge only rebuilds when `client.is_connected` is still false;
that check requires full MQTT transport, gateway, and vessel readiness.

The rebuild disconnects the stale client, obtains a fresh broker URL, creates a
new transporter and `GeckoIotClient`, and starts the normal initial connection
worker. Transient setup failures retry with exponential backoff capped at five
minutes. Only an OAuth authentication error changes the session to
`reauth_required`; other failures retain the session and do not publish
`login_required`. Recovery is cancelled during shutdown, reauthentication, or
when a newer client supersedes the affected one.

### Two Details to Consider When Analyzing

**`gecko/auth/status` is not the transport readiness signal.** It remains
`authenticated` during the recovery delay, and changes to `authenticating` once
a replacement client is being built. A successful replacement returns it to
`authenticated`. Use connectivity for operational readiness.
As a signal for "connected", use `gecko/status/connectivity`, which the library
updates correctly on the first connection loss.

**`client.is_connected` is stricter than "MQTT is alive".** It returns
`is_fully_connected`, meaning MQTT transport **and** Gateway **and** Vessel. The
heat-pump watchdog therefore counts this as a failure even when the transport
is up but cloud login has not yet completed.
