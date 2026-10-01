# gecko-pool-mqtt

Standalone Python bridge between a Gecko in.touch 3 pool controller and a local
MQTT broker.

The service uses `gecko-iot-client` to connect to the Gecko cloud and exposes the
data locally through a second, independent MQTT client:

- Gecko cloud: the library's internal AWS IoT MQTT connection
- Local broker: MQTT connection to `MQTT_HOST` under the `gecko/` topic prefix

There is no HTTP, FastAPI, or Node-RED interface.

## Gecko Library and API Version

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

## Prerequisites

- Docker and Docker Compose
- Access to the local MQTT broker
- Gecko account and pool controller
- An MQTT client such as `mosquitto_pub` and `mosquitto_sub` for the initial login

## Configuration

Copy the configuration template:

```text
copy .env.example .env
```

Important settings in `.env`:

| Variable | Default | Description |
|---|---:|---|
| `MQTT_HOST` | `mqtt.example.com` | Hostname of the local broker |
| `MQTT_PORT` | `1883` | Port of the local broker |
| `MQTT_BASE_TOPIC` | `gecko` | Prefix for all bridge topics |
| `MQTT_CLIENT_ID` | `gecko-pool-mqtt` | MQTT client ID |
| `MQTT_SHUTDOWN_PUBLISH_TIMEOUT` | `2.0` | Time to wait during shutdown until retained `offline` is confirmed |
| `MQTT_USERNAME` | empty | Optional username |
| `MQTT_PASSWORD` | empty | Optional password |
| `OAUTH_TOKEN_FILE` | `/data/tokens.json` | Persistent token path |
| `GECKO_ACCOUNT_ID` | empty | Optional: skip account discovery |
| `GECKO_MONITOR_ID` | empty | Optional: force vessel selection |
| `GECKO_CONFIG_TIMEOUT` | `30.0` | Timeout for the Gecko configuration |
| `GECKO_HEAT_PUMP_FLOW_ZONE_ID` | `4` | Flow zone ID of the pump used for external heat pumps |
| `GECKO_HEAT_PUMP_DEFAULT_DURATION` | `30` | Default runtime of the heat-pump request in minutes |
| `GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS` | `2` | Maximum failed reassert attempts before the emergency stop; `0` = unlimited |
| `GECKO_HEAT_PUMP_CHECK_INTERVAL` | `5.0` | Heat-pump watchdog check interval in seconds |
| `GECKO_HEAT_PUMP_CONFIRM_TIMEOUT` | `15.0` | Maximum wait for the activation confirmation in seconds |
| `LOG_LEVEL` | `INFO` | Python log level |

### Migrating from the former topic prefix

The default base topic changed from `geeko` to `gecko`, so that the published
topics match the spelling of the manufacturer. This breaks an installation that
is already running. Two ways to deal with it:

- **Move the subscriptions.** Change every MQTT subscription and automation from
  `geeko/…` to `gecko/…`. Home Assistant's MQTT integration lists each topic in
  its discovery payload, so the new entities appear on their own after the
  bridge restarts. The old retained messages stay on the broker as leftovers.
- **Keep the old topics.** Set `MQTT_BASE_TOPIC=geeko` in `.env` and nothing
  else moves. Note that an explicit entry in `.env` always wins over the default
  in `app/config.py`.

Trace files written before the rename still contain `geeko/…` topics and are no
longer directly comparable with new ones. The file names are unaffected.

The registered OAuth redirect URL is:
`https://my.home-assistant.io/redirect/oauth`.

Do not treat `GECKO_OAUTH2_CLIENT_ID` as a secret: it is Gecko Alliance's
public PKCE client without a client secret, the same one used by the official
Home Assistant integration
[`geckoal/ha-gecko-integration`](https://github.com/geckoal/ha-gecko-integration).
PKCE replaces the client secret with a cryptographic code challenge, so the value
does not need protection. It is a code default so the service runs after a
`git clone` without an additional step. If you registered a custom client,
override the variable in `.env`.

### Configuration Validation Is Not Fatal

`app/main.py` calls `settings.validate()`, logs any errors found, and then
starts the service **anyway**. Validation is therefore advisory, not enforced.
Specific consequence for heat pump timings:

- If `GECKO_HEAT_PUMP_CONFIRM_TIMEOUT` is smaller than
  `GECKO_HEAT_PUMP_CHECK_INTERVAL`, validation reports this with
  `GECKO_HEAT_PUMP_CONFIRM_TIMEOUT must be at least GECKO_HEAT_PUMP_CHECK_INTERVAL`,
  but the service continues running. Practical effect: The confirmation deadline
  may already have expired at the first watchdog check, causing an earlier
  reassert than intended.
- Likewise, `GECKO_HEAT_PUMP_CHECK_INTERVAL <= 0` and
  `GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS < 0` are only reported, not prevented.

Anyone who intentionally sets tight timings should consciously ignore the warning
in the log (`Configuration errors: ...`).

It is also checked that `GECKO_HEAT_PUMP_CONFIRM_TIMEOUT` is at least 5
seconds. This is not an arbitrary limit: The library's zone methods wait up to
5 seconds for the PUBACK acknowledgment. A reassert can therefore remain in the
call for 5 seconds, and with a smaller window the confirmation deadline expires
during the call, causing the watchdog to reassert again even though Gecko could
not yet respond. The value belongs to the library, not the bridge;
`tests/test_library_contract.py` pins the version it comes from.

## Start with Docker Compose

```text
copy .env.example .env
docker compose up -d --build
docker compose logs -f gecko-mqtt
```

The service exposes no ports. The token is stored in the Docker volume
`gecko-tokens` under `/data/tokens.json`. The Compose setup expects an already
running external MQTT broker and does not start its own broker.

Check status:

```text
mosquitto_sub -h mqtt.example.com -t "gecko/#" -v
```

## OAuth Login over MQTT

The PKCE login requires a running process because the code verifier and
state are kept only in memory.

1. Start the service and wait for `gecko/auth/challenge`.
2. Open the `authorize_url` value from the retained challenge in a browser.
3. Log in to Gecko.
4. After the redirect, copy the complete URL from the browser address bar.
5. Send the URL to `gecko/auth/response`:

```text
mosquitto_pub -h mqtt.example.com -t gecko/auth/response -m "https://my.home-assistant.io/redirect/_change/?redirect=oauth%2F%3Fcode%3D...%26state%3D..."
```

Alternatively, `gecko/auth/response` accepts JSON:

```json
{"redirect_url":"https://my.home-assistant.io/redirect/oauth?code=...&state=..."}
```

Or directly:

```json
{"code":"...","state":"..."}
```

After successful login:

- `gecko/auth/status` becomes `{"status":"authenticated",...}`
- `gecko/auth/challenge` is deleted as an empty retained message
- Zone and connectivity statuses are published

A new login challenge can be requested at any time:

```text
mosquitto_pub -h mqtt.example.com -t gecko/auth/login -m ""
```

## MQTT Topics

All topics start with `gecko/` by default. With a different
`MQTT_BASE_TOPIC`, replace this prefix accordingly.

### Authentication

| Topic | Direction | Retained | Payload |
|---|---|---:|---|
| `gecko/auth/status` | Bridge → Broker | Yes | JSON with `status` and `reason` |
| `gecko/auth/challenge` | Bridge → Broker | Yes | JSON with `authorize_url`, `state`, `instructions` |
| `gecko/auth/response` | Broker → Bridge | No | Redirect URL or JSON code/state |
| `gecko/auth/login` | Broker → Bridge | No | Any payload; creates a new challenge |

Possible auth status values are `login_required`, `authenticating`,
`authenticated`, and `reauth_required`.

### Status

Status messages are JSON and are published retained.

| Topic | Payload |
|---|---|
| `gecko/status/availability` | `online` or `offline` |
| `gecko/status/connectivity` | Gecko connectivity data plus auth fields |
| `gecko/status/operation_mode` | Current Watercare/Operation mode |
| `gecko/status/zone/temperature/<zone_id>` | Temperature zone with current and target value |
| `gecko/status/zone/lighting/<zone_id>` | Lighting zone with activity, color, and effect |
| `gecko/status/zone/flow/<zone_id>` | Flow zone with activity, speed, run reason, and presets |
| `gecko/status/zones` | Complete snapshot by zone type |
| `gecko/status/heatPump/state` | Heat pump request state |

Example of a temperature zone:

```json
{
  "id": "zone-1",
  "name": "Pool",
  "type": "temperature",
  "state": {
    "current_temperature": 27.5,
    "target_temperature": 28.0,
    "status": "READY",
    "eco_mode": false,
    "min_set_point": 15.0,
    "max_set_point": 40.0
  }
}
```

Example of `gecko/status/heatPump/state`:

```json
{
  "state": "waiting_confirmation",
  "zone_id": "4",
  "armed": true,
  "error_count": 0,
  "max_errors": 2,
  "remaining_seconds": 1794,
  "timestamp": "2026-09-30T06:30:00+00:00"
}
```

| Field | Meaning |
|---|---|
| `state` | `disarmed`, `waiting_confirmation`, `running`, `disconnected`, `error`, or `disarmed_expired` |
| `zone_id` | Flow zone ID in use |
| `armed` | `true` while the controller wants to keep the zone active |
| `error_count` | Failed reassert/confirmation attempts for the current request |
| `max_errors` | Value of `GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS`; `0` = emergency stop disabled |
| `remaining_seconds` | Remaining time in **seconds**, otherwise `null` |
| `timestamp` | UTC timestamp of publication |

Units: `remaining_seconds` is in seconds, while the `duration` field of the
`gecko/cmd/heatPump` command and `GECKO_HEAT_PUMP_DEFAULT_DURATION` are in
**minutes**. For `disarmed`, `error`, and `disarmed_expired`,
`remaining_seconds` is `null`.

An emergency stop always results in `error` with `armed: false`, regardless of
whether it was triggered by the confirmation timeout or the offline branch.

`state` and `armed` never contradict each other: `disarmed`, `error`, and
`disarmed_expired` correspond to `armed: false` with `remaining_seconds: null`,
while `waiting_confirmation`, `running`, and `disconnected` correspond to
`armed: true` with remaining time.

The topic is published retained. While the watchdog is running, it is also
updated in every `GECKO_HEAT_PUMP_CHECK_INTERVAL` cycle – **exactly once per
cycle**, not multiple times with an identical payload. A state change within a
cycle, such as `disconnected` to `running`, is additionally published
immediately. A new `active: true` when `running` is already active is **not** a
state change and is therefore not published again – Gecko provides zone updates
for all zones, and otherwise every switch of light or pump 1 would produce an
additional `running` line.
The state transitions and error cases are
described in
[`heatpump_sm.md`](heatpump_sm.md).

The two heat pump events `gecko/status/heatPump/reassert` and
`gecko/status/heatPump/error`, by contrast, are **not** retained and are
therefore not included in this table.

### Pump Run Reason

Flow zones contain the initiator codes supplied by Gecko in the
`state.initiators` field. `state.initiator_labels` additionally provides
readable names. This makes it especially clear whether a pump is running because
of a filter cycle or a cooldown.

The flow status also contains the hardware capabilities:

```json
{
  "supports_speed_percentage": false,
  "supports_turn_on": true,
  "supports_turn_off": true,
  "speed_config": null,
  "capabilities": ["supports_turn_on", "supports_turn_off"]
}
```

For a pump with `supports_speed_percentage: false`, no `speed` value may be
sent. The bridge rejects such a command with `success: false` and a
corresponding error message. For a pure on/off pump, the correct
command is:

```json
{"action":"on"}
```

A variable-speed pump additionally provides a `speed_config`, for example:

```json
{
  "supports_speed_percentage": true,
  "speed_config": {
    "minimum": 20,
    "maximum": 100,
    "stepIncrement": 10
  }
}
```

This command is then valid, for example:

```json
{"action":"on","speed":20}
```

| Code | Label | Meaning |
|---|---|---|
| `FI` | `filtration` | Filter cycle |
| `CD` | `cooldown` | Cooldown / cooling cycle |
| `HT` | `heating` | Heating |
| `HTP` | `heat_pump` | Heat pump |
| `PU` | `purge` | Flush/purge cycle |
| `CF` | `checkflow` | Flow check |
| `UD` | `user_demand` | Manual user demand |

Example:

```json
{
  "id": "1",
  "name": "Pump 1",
  "type": "flow",
  "state": {
    "active": true,
    "speed": 50,
    "initiators": ["FI"],
    "initiator_labels": ["filtration"],
    "presets": []
  }
}
```

When multiple causes occur simultaneously, multiple entries are transmitted, for
example `initiators: ["HT", "FI"]`. The codes intentionally remain in the
payload so the values can be traced unambiguously to the Gecko app. Unknown
codes are labeled `unknown:<code>`.

### Commands

Commands are sent as JSON to `gecko/cmd/+/+/set`. For each command, the bridge
publishes a response under
`gecko/cmd/<type>/<zone_id>/result`.

Temperature:

```text
mosquitto_pub -h mqtt.example.com -t gecko/cmd/temperature/zone-1/set -m '{"target_temperature":28.0}'
```

Turn lighting on:

```json
{"action":"on","r":255,"g":120,"b":40,"intensity":200}
```

Turn lighting off:

```json
{"action":"off"}
```

Set lighting effect:

```json
{"action":"on","effect":"rainbow"}
```

Turn flow on:

```json
{"action":"on","speed":50}
```

This command may only be used when the flow zone reports
`supports_speed_percentage: true` in its status. For an on/off pump, the speed
value must be omitted:

```json
{"action":"on"}
```

If a `speed` value is nevertheless sent to a non-variable-speed pump, the bridge
responds under the respective `result` topic with `success: false`, for example:

```json
{
  "success": false,
  "message": "Flow zone 1 supports on/off only; speed percentage is not supported",
  "zone_id": "1"
}
```

### Example: Turn On Pump 4

The zone ID is not necessarily included in the display name. First list the flow
zones:

```text
mosquitto_sub -h mqtt.example.com -t 'gecko/status/zone/flow/+' -v
```

Alternatively, subscribe to the complete snapshot:

```text
mosquitto_sub -h mqtt.example.com -t gecko/status/zones -v
```

If the fourth pump has ID `4`, it can be turned on at a fixed speed
of 50 percent:

```text
mosquitto_pub -h mqtt.example.com \
  -t gecko/cmd/flow/4/set \
  -m '{"action":"on","speed":50}'
```

In Windows PowerShell, run the command on one line because `\` is not a
line-continuation character there:

```powershell
mosquitto_pub -h mqtt.example.com -t gecko/cmd/flow/4/set -m '{"action":"on","speed":50}'
```

Without specifying a speed, the pump is simply activated:

```text
mosquitto_pub -h mqtt.example.com \
  -t gecko/cmd/flow/4/set \
  -m '{"action":"on"}'
```

PowerShell:

```powershell
mosquitto_pub -h mqtt.example.com -t gecko/cmd/flow/4/set -m '{"action":"on"}'
```

The bridge publishes the response to:

```text
gecko/cmd/flow/4/result
```

To observe the ack at the same time:

```text
mosquitto_sub -h mqtt.example.com -t 'gecko/cmd/flow/4/result' -v
```

A successful result looks like this, for example:

```json
{
  "success": true,
  "message": "Flow command applied",
  "zone_id": "4"
}
```

A successful `result` ack means that the desired state has been passed to the
Gecko connection. Always also check the actually applied state on the retained
status topic, because the pool controller may reject a desired state or execute
it differently due to an automatic initiator such as `FI`
or `CD`.

After switching it on, check the actual status:

```text
mosquitto_sub -h mqtt.example.com -t 'gecko/status/zone/flow/4' -v
```

Example status with a run reason:

```json
{
  "id": "4",
  "name": "Pump 4",
  "type": "flow",
  "state": {
    "active": true,
    "speed": 50,
    "initiators": ["UD"],
    "initiator_labels": ["user_demand"],
    "presets": []
  }
}
```

If the pump is running as part of an automatic cycle, the following values may
appear instead, for example:

```json
{
  "initiators": ["FI"],
  "initiator_labels": ["filtration"]
}
```

```json
{
  "initiators": ["CD"],
  "initiator_labels": ["cooldown"]
}
```

A pump is switched off with:

```text
mosquitto_pub -h mqtt.example.com \
  -t gecko/cmd/flow/4/set \
  -m '{"action":"off"}'
```

The Gecko client may reject the shutdown if the pump is currently activated not
by a user request but, for example, by `FI` (`filtration`) or `CD` (`cooldown`).
In this case, the result topic contains `success: false` and the controller's
reason.

Turn on flow without specifying a speed:

```json
{"action":"on"}
```

Turn off flow:

```json
{"action":"off"}
```

The color channels and `intensity` each range from `0` to `255`. `action` must
be `on` or `off`. Invalid payloads produce an error ack and do not stop the
service.

Example of a successful ack:

```json
{"success":true,"message":"Target temperature set to 28.0","zone_id":"zone-1"}
```

### External Heat Pump: Minimum Runtime

For an external heat pump, the configured flow zone can be activated for at
least a defined period using a dedicated command. Flow zone `4` is used by
default; its ID can be changed with
`GECKO_HEAT_PUMP_FLOW_ZONE_ID`.

The complete state machine, status payloads, and error transitions are in
[`heatpump_sm.md`](heatpump_sm.md).

The command deliberately uses its own topic `gecko/cmd/heatPump`
(without `/set`):

```powershell
mosquitto_pub -h mqtt.example.com -t gecko/cmd/heatPump -m '{"action":"on","duration":30}'
```

`duration` is specified in minutes. If `duration` is omitted,
`GECKO_HEAT_PUMP_DEFAULT_DURATION` is used (30 minutes by default).
Further `on` commands extend the active request but do not shorten it.
During the request, the bridge monitors the flow status and the Gecko
initiators. If, for example, `FI` is removed at the end of a filter cycle
and the pump therefore switches off, `active: true` is passed to Gecko
again.

A running request can be stopped immediately:

```powershell
mosquitto_pub -h mqtt.example.com -t gecko/cmd/heatPump -m '{"action":"off"}'
```

The result is published under `gecko/cmd/heatPump/result`. Example of a
successful `on` ack:

```json
{"success":true,"message":"Heat-pump pump request active","action":"on","zone_id":"4","duration":32,"remaining_seconds":1920,"state":"running"}
```

| Field | Meaning |
|---|---|
| `success` | `true` if the request was armed |
| `message` | Short plain-text result |
| `action` | `on` or `off` |
| `zone_id` | Flow zone ID used |
| `duration` | Requested minimum runtime in **minutes** |
| `remaining_seconds` | Remaining runtime in **seconds** |
| `state` | State produced by **this** command: `running` for an already active zone, otherwise `waiting_confirmation` |

`state` deliberately describes the command transition, not the state before it.
Therefore, an `on` immediately after a restart reports `waiting_confirmation`
or `running`, never the initial state's `disarmed`.

The bridge only switches
the Gecko pump; the external heat pump itself requires separate control and its
own flow/safety protection.

### A Restart Discards the Request

After a bridge restart, **no** heat-pump request is
resumed. The controller starts `disarmed`, no watchdog runs, and it does not
reconstruct anything from the cloud state. Specifically:

- `gecko/status/heatPump/state` reports `disarmed` once with `armed: false`
  and `remaining_seconds: null`.
- Until a new `on` has been received, the controller ignores every zone update.
  Switching the pump off through the Gecko app also triggers **no** reassert,
  because `_observe_zone_update` exits when `not self._armed`.
- An initiator still set in the cloud remains in place. Shutdown deliberately
  sends **no** `deactivate()`, so maintenance does not cut off a running filter
  cycle. The zone then reports `armed: false` while the pump is running,
  indistinguishable in `status/heatPump/state` from "the pump is intentionally
  off".
- The pump remains off until the calling automation sends again. With a
  30-minute interval and `duration: 32`, this means up to 30 minutes without
  heat-pump flow.

To verify this without waiting for the next filter cycle:

```text
mosquitto_pub -h mqtt.example.com -t gecko/cmd/heatPump -m '{"action":"on","duration":10}'
docker compose restart
mosquitto_sub -h mqtt.example.com -t "gecko/status/heatPump/#" -v -W 20
```

Exactly one `state` line with `disarmed` is expected, followed by nothing else.
Now switch the pump off through the app: there must be **no** `reassert`.
The automation's next `on` switches it on again.

Both behaviors are intentional and are deliberately not being fixed because
each solution would introduce its own failure mode:

- **Reconstruct the deadline from retained state.** Read
  `gecko/status/heatPump/state` at startup and re-arm with the remaining
  `remaining_seconds` when `armed: true`. No additional file is needed, but this
  survives only while the retained topic is not deleted and produces an already
  expired request after a long outage.
- **Persist the request alongside the tokens on `/data`.** Write the command,
  deadline, and `error_count` on `on`; delete them on `stop`, `off`, expiration,
  and emergency stop. This also survives `docker compose down` and a rebuild,
  but requires the same persistence maintenance as `tokens.json`.

Each reassert attempt or failed confirmation attempt is published as a
non-retained event under `gecko/status/heatPump/reassert`:

```json
{
  "timestamp": "2026-09-28T11:38:22.123456+00:00",
  "zone_id": "4",
  "reason": "zone became inactive",
  "initiators": [],
  "attempt": 1,
  "activate_called": true,
  "activate_error": null,
  "confirmed": false
}
```

`attempt` corresponds to the error counter for the current heat-pump request.
It is `0` on the first reassert while no error has yet been counted, and
increases from the first confirmed error onward. The first `action: on` does
not count as a reassert. With an existing Gecko connection, `activate()` is
called again (`activate_called: true`). This also applies when the Gecko
connection is currently unconfirmed, because the client may buffer the desired
state or transmit it later. An error that occurred is recorded in
`activate_error`; a `null` only means that the method call returned
successfully. `active: true` must still be confirmed afterward. An attempt is
confirmed only by a subsequent `gecko/status/zone/flow/<zone_id>` update with
`state.active: true`; the counter is then reset. The watchdog check and regular
publication of `gecko/status/heatPump/state` run at the interval specified by
`GECKO_HEAT_PUMP_CHECK_INTERVAL`.

`confirmed` is `true` when Gecko has already confirmed activation during the
blocking `activate()` call, before the zone-update callback could run on the
event loop. The controller then reports `running` directly and never
`waiting_confirmation` for a zone that is already running. In all other cases,
`confirmed: false` remains set, and confirmation follows through the
zone update.

Confirmation relies exclusively on `state.active`, without checking the
initiator. Activity from `FI` or `CD` therefore also counts as confirmation.
Details and limitations are documented in
[`heatpump_sm.md`](heatpump_sm.md) under *Limits of the Activity Check*.

Possible values for `reason`:

| `reason` | Trigger |
|---|---|
| `zone became inactive` | Watchdog check: zone inactive despite `armed` |
| `zone reported inactive` | Zone update with `active: false` during an active request |
| `activation not confirmed within confirm_timeout` | `active: true` did not arrive in time; `activate_called: false` |
| `activate failed: <exception>` | The `activate()` call itself raised an exception |
| `gecko disconnected` | Watchdog check without a complete Gecko connection |
| `watchdog exception: <exception>` | Error during watchdog or zone lookup |

With a disconnected Gecko connection, `activate()` is not called. Instead, the
`disconnected` state is published and the offline error counter is incremented
at most once per watchdog cycle. This is also counted when the client is
completely absent. These errors count toward
`GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS` and can trigger an emergency stop; with
`GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS=0`, no emergency stop occurs.
Two details matter during an outage: Reconnecting does **not** reset the error
counter; only a confirmation, `off`, or a new `on` does. An unstable connection
can therefore continue the emergency stop. The state remains `disconnected`
until then.

Reassert events can be observed with this command:

```powershell
mosquitto_sub -h mqtt.example.com -t 'gecko/status/heatPump/reassert' -v
```

If the pump remains inactive after the configured number of failed reassert
attempts, the bridge terminates the internal heat-pump request as an emergency
stop and publishes one non-retained error event under
`gecko/status/heatPump/error`. With
`GECKO_HEAT_PUMP_MAX_REASSERT_ATTEMPTS=0`, the emergency stop remains disabled and
the bridge continues trying indefinitely:

```json
{
  "timestamp": "2026-09-28T11:39:22.123456+00:00",
  "zone_id": "4",
  "error": "emergency_stop",
  "reason": "Pump not confirmed after 2 attempts: activation not confirmed within confirm_timeout",
  "attempts": 2,
  "initiators": []
}
```

`reason` always has the form `Pump not confirmed after <n> attempts: <trigger>`.
The `<trigger>` part is one of the `reason` values from the
reassert table above; in this example, it is the expired
confirmation deadline.

The error event can be received only if the subscriber is already subscribed
before the emergency stop because it is published as non-retained:

```powershell
mosquitto_sub -h mqtt.example.com -t 'gecko/status/heatPump/error' -v
```

The reassert counter is reset on `active: true`, `action: off`, expiration of
the request, or a new request. An emergency stop terminates the current
request; a later `action: on` starts a new count.

## MQTT Trace

To analyze operating histories without an external recorder, the bridge writes
every message it processes as one JSON line per day in
`MQTT_TRACE_DIR`. This covers outgoing messages – status, acks, and events –
as well as incoming commands. The trace is a passive observer and does not
alter the flow.

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_TRACE_DIR` | `/tmp/mqtt` | Target directory. Empty or `off` disables tracing. |
| `MQTT_TRACE_RETENTION_DAYS` | `7` | Retention in days. `0` keeps everything. |

`docker-compose.yml` binds `/tmp/mqtt` to `./mqtt-trace` in the project directory.
Without this mount, the file would be in the container's writable layer and
would be accessible only via `docker cp`. The directory is excluded in `.gitignore`.

Example of a line:

```json
{"ts":"2026-05-04T11:02:49.076556+00:00","dir":"out","topic":"gecko/status/heatPump/reassert","payload":"{\"reason\":\"zone reported inactive\"}","qos":1,"retain":false}
```

| Field | Meaning |
|---|---|
| `ts` | UTC timestamp with microseconds |
| `dir` | `in` for received, `out` for sent |
| `topic` | Complete topic including the base prefix |
| `payload` | Payload as raw text, not reformatted |
| `qos`, `retain` | Only with `dir: out` |
| `bytes` | Only with `auth/response`: length of the redacted payload |

The payload of `auth/response` contains the OAuth code from the redirect and is
replaced with `<redacted>`. The message continues unchanged; only the recording
is truncated.

Files are named `mqtt-YYYY-MM-DD.jsonl` and rotate at midnight UTC. On each
rotation, older files are deleted according to `MQTT_TRACE_RETENTION_DAYS`.
With the default `GECKO_HEAT_PUMP_CHECK_INTERVAL=20`, about 2 MB are created per
day, or about 14 MB with seven days of retention.

### Analysis

All state changes of the heat-pump controller in chronological order:

```powershell
jq.exe -r 'select(.topic=="gecko/status/heatPump/state") | "\(.ts) \(.payload|fromjson|.state)"' mqtt-trace/mqtt-2026-09-30.jsonl
```

Find duplicate publications of the same state line, meaning two
lines less than one second apart:

```powershell
jq.exe -r 'select(.topic=="gecko/status/heatPump/state") | .ts' mqtt-trace/mqtt-2026-09-30.jsonl
```

What the bridge received while the filter cycle was running:

```powershell
jq.exe -r 'select(.dir=="in") | "\(.ts) \(.topic) \(.payload)"' mqtt-trace/mqtt-2026-09-30.jsonl
```

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

## Connection and Reconnect

Two completely different mechanisms must be distinguished.

### Initial Connection: Bridge Retry Loop

`PoolController._connect_worker` calls `client.connect()` in a daemon thread
and retries on failure with exponential backoff: 5 s, 10 s, 20 s, up to a
maximum of 300 s (`GECKO_CONFIG_TIMEOUT` additionally limits each individual
attempt). The loop continues indefinitely.

This applies **only to the initial connection**. Once `connect()` has succeeded
once, the thread returns and is not restarted.

### Runtime Failure: Library Reconnect

The bridge is not responsible for a later connection loss. The library detects
the loss through an awscrt lifecycle callback and
reconnects automatically:

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
The connection therefore recovers on its own; a container restart is not
necessary. `restart: unless-stopped` only applies after a process crash.

### Two Details to Consider When Analyzing

**`gecko/auth/status` does not reflect an outage.** It remains `authenticated`
throughout the outage because the value is set only when `_connect_worker`
succeeds, and that worker has long since finished. The connection recovers on
its own, but the bridge's auth status does not report it.
As a signal for "connected", use `gecko/status/connectivity`, which the library
updates correctly on the first connection loss.

**`client.is_connected` is stricter than "MQTT is alive".** It returns
`is_fully_connected`, meaning MQTT transport **and** Gateway **and** Vessel. The
heat-pump watchdog therefore counts this as a failure even when the transport
is up but cloud login has not yet completed.

## Troubleshooting

### `login_required` Remains Active

Read the challenge, open the URL in a browser, and send the full redirect URL
back to `gecko/auth/response`. Generate a new challenge:

```text
mosquitto_pub -h mqtt.example.com -t gecko/auth/login -m ""
```

### `reauth_required`

The refresh token was rejected or has expired. A new challenge is
published automatically. After a successful login, the existing
tokens are replaced.

### `offline` or No MQTT Status

Check broker reachability, `MQTT_HOST`, `MQTT_PORT`, and optional credentials.
The bridge uses MQTT reconnects. The Last Will topic marks an unexpected
connection loss as `offline`.

If `status/availability` remains `online` after a clean stop, check whether the
service actually shut down normally. A forced container stop or a crash is not
covered by the Last Will because it is published only after an unexpected
connection loss. During an orderly shutdown, the bridge waits up to
`MQTT_SHUTDOWN_PUBLISH_TIMEOUT` seconds for confirmation of the retained
`offline` message before disconnecting. Integration test `TC-INT-05-01` checks
exactly this case.

### No Zone Status Data

First check `gecko/status/connectivity`. The Gecko IoT connection is established
in a daemon thread and may need some time for initial configuration after a
successful OAuth login.

After an error during the initial Gecko connect, the bridge automatically tries
to reconnect. The wait increases from five seconds to a maximum of five
minutes. Meanwhile, the local MQTT broker remains available and
`gecko/auth/status` remains `authenticating` until the Gecko connection and
configuration have been loaded successfully.

After a successful connect, the `gecko-iot-client` transport also handles
reconnection, including JWT refresh and backoff. An invalid refresh token
instead leads to `reauth_required` and a new OAuth challenge. The
wait sequence and limits are documented
under
[Connection and Reconnect](#connection-and-reconnect).

### Flow Command Is Not Executed

First check whether the zone has the specified ID and is fully
connected:

```text
mosquitto_sub -h mqtt.example.com -t 'gecko/status/zone/flow/+' -v
mosquitto_sub -h mqtt.example.com -t gecko/status/connectivity -v
```

For a pump without speed control, use only this command:

```text
mosquitto_pub -h mqtt.example.com -t gecko/cmd/flow/1/set -m '{"action":"on"}'
```

If `speed` is sent, the flow status must first contain
`supports_speed_percentage: true`. The response is published to:

```text
gecko/cmd/flow/1/result
```

With Docker, the bridge reception and Gecko forwarding logs can be
checked:

```text
docker compose logs -f gecko-mqtt
```

These log entries are expected (strings exactly as in the code):

| Log entry | Source | Meaning |
|---|---|---|
| `Connected to local MQTT broker` | `app/mqtt_bridge.py` | Connection to the local broker is established |
| `Received local MQTT message` | `app/mqtt_bridge.py` | Message has arrived |
| `Dispatching command` | `app/mqtt_bridge.py` | Command was validated and forwarded |
| `Dispatching heatPump command` | `app/mqtt_bridge.py` | Heat-pump command was forwarded |
| `Handling flow/control command` | `app/pool_controller.py` | Command is being processed |
| `Gecko client ready for command` | `app/pool_controller.py` | Gecko is connected; command is sent to the library |
| `Applying flow command` | `app/pool_controller.py` | Flow command is applied to the zone |
| `GeckoIotClient connected successfully` | `app/pool_controller.py` | Gecko Cloud connection is established |
| `Command failed` | `app/pool_controller.py` | Command failed, `result` with `success: false` |

If `Received local MQTT message` is already missing, the broker, topic prefix,
or sender's MQTT credentials do not match the bridge. If `Gecko client ready for
command` is missing, the Gecko connection is not ready yet. If
`GeckoIotClient connect failed: ...; retrying in Ns` appears, reconnect backoff
is active (5 seconds to a maximum of 5 minutes).

## Validation

The bridge is tested with `pytest`. The complete requirements and test-case
overview is in [`REQUIREMENTS.md`](REQUIREMENTS.md). The
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

368 unit tests and 9 integration tests pass. The
`gecko-iot-client` library is not tested for its behavior, but it is tested
against the contract that `app/` has with it.

### Manual Smoke Tests

These cases require a real Gecko and are tracked as `TC-MAN-xx` in
[`REQUIREMENTS.md`](REQUIREMENTS.md).

Observe all topics during the test:

```text
mosquitto_sub -h mqtt.example.com -t "gecko/#" -v
```

If you do not want to run a second client, use the
[MQTT trace](#mqtt-trace): It contains outgoing and
incoming messages with timestamps and can be fully
analyzed after the test.

Temperature, light, and flow:

| Step | Command | Expected |
|---|---|---|
| Set temperature | `gecko/cmd/temperature/<zone_id>/set` with `{"target_temperature":28.0}` | `result` with `success: true`, then `status/zone/temperature/<zone_id>` shows the target value |
| Light on | `gecko/cmd/lighting/<zone_id>/set` with `{"action":"on","r":255,"g":120,"b":40}` | `success: true`, color in `status/zone/lighting/<zone_id>` |
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
| Re-arm | `status/heatPump/error` was not subscribed: `gecko/status/heatPump/state` shows `state: error` with `armed: false` after the emergency stop; then `{"action":"on"}` | New cycle, `error_count` starts again at `0`, state changes to `waiting_confirmation` |
| Off | `{"action":"off"}` | `state: disarmed`, `armed: false`; flow zone is inactive |
| Shutdown | `docker compose stop` | `status/availability` changes retained from `online` to `offline` |

Also verify that a deliberately slow blocking Gecko call does not halt the
event loop: during `waiting_confirmation`, watchdog cycle updates and incoming
MQTT commands must continue to run.

## Architecture

```text
Gecko Cloud / AWS IoT
        │ gecko-iot-client
        ▼
   PoolController
        │ paho-mqtt
        ▼
Local MQTT broker
        │
      gecko/#
```

The Gecko client's callbacks originate from background threads. Status publishes
from the local Paho client are thread-safe; OAuth and command operations are
delegated back to the asyncio event loop.

### Thread Boundaries

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
