# pybyd

Async Python client for the BYD vehicle API. Provides typed access to
vehicle telemetry, GPS location, energy consumption, and remote control
commands (lock, unlock, climate, horn, flash).

Built on top of the protocol research from
[Niek/BYD-re](https://github.com/Niek/BYD-re) and the endpoint
definitions found in [TA2k/ioBroker.byd](https://github.com/TA2k/ioBroker.byd).

**Status:** Alpha. The API surface may change before 1.0.

## PLEASE READ FIRST!

We are still working out the kinks, especially mapping states and setting up parsing. See [API_MAPPING.md](https://github.com/jkaberg/pyBYD/blob/main/API_MAPPING.md) for the current state.

Any help around this is greatly appreciated, use the [test client](https://github.com/jkaberg/pyBYD#dump-all-data) to fetch the car values and send an PR. 

Thanks!

## Feature highlights

- Polling support for live vehicle metrics (realtime, GPS, energy, charging, HVAC).
- Remote command support for lock/unlock, horn/lights, and climate actions.
- Deterministic per-vehicle state store that merges partial responses predictably
- MQTT-assisted updates that enrich the state store between HTTP polls
- Typed models implemented with Pydantic

## Requirements

- Python 3.11+
- aiohttp
- cryptography
- paho-mqtt

## Installation

```bash
pip install pybyd
```

Or install from source:

```bash
pip install -e ".[dev]"
```

## Quick start

```python
import asyncio
from pybyd import BydClient, BydConfig

async def main():
    config = BydConfig(
        username="you@example.com",
        password="your-password",
        country_code="NL",
    )

    async with BydClient(config) as client:
        vehicles = await client.get_vehicles()

        vin = vehicles[0].vin
        print(f"VIN: {vin}")

        realtime = await client.get_vehicle_realtime(vin)
        print(f"Battery: {realtime.elec_percent}%")
        print(f"Range: {realtime.endurance_mileage} km")

        gps = await client.get_gps_info(vin)
        print(f"Location: {gps.latitude}, {gps.longitude}")

asyncio.run(main())
```

## Available endpoints

| Method | Description |
|--------|-------------|
| `login()` | Authenticate and obtain session tokens |
| `get_vehicles()` | List all vehicles on the account |
| `get_vehicle_realtime(vin)` | Battery, range, speed, doors, tire pressure |
| `get_gps_info(vin)` | GPS latitude, longitude, speed, heading |
| `get_energy_consumption(vin)` | Energy and fuel consumption stats |
| `verify_command_access(vin)` | Verify control PIN and enable remote commands |
| `lock(vin)` | Lock doors |
| `unlock(vin)` | Unlock doors |
| `flash_lights(vin)` | Flash lights |
| `find_car(vin)` | Find my car |
| `start_climate(vin)` | Start climate control |
| `stop_climate(vin)` | Stop climate control |
| `schedule_climate(vin, ...)` | Schedule climate control |
| `set_seat_climate(vin, ...)` | Seat heating/ventilation |
| `set_battery_heat(vin, on=...)` | Battery heating |

## Configuration

Credentials can be passed directly or read from environment variables:

```python
# From environment: BYD_USERNAME, BYD_PASSWORD, BYD_COUNTRY_CODE, ...
config = BydConfig.from_env()

# With overrides
config = BydConfig.from_env(country_code="DE", language="de")
```

All `BYD_*` environment variables listed in `BydConfig.from_env` are
supported for CI and container deployments.

## Remote control

Remote commands require a control PIN configured as `BydConfig(control_pin=...)`
or passed explicitly via `command_pwd=...`.

```python
# Lock/unlock
result = await client.lock(vin)
print(result.success)

# Climate control
result = await client.start_climate(vin, temperature_c=21.0, time_span=10)

# Hass-byd style presets
await client.start_climate(vin, preset="max_heat", time_span=10)
await client.start_climate(vin, preset="max_cool", time_span=10)

# Note: BYD encodes duration as a small code; pyBYD accepts minutes (10/15/20/25/30)
# and converts internally.
```

Remote commands use a two-phase trigger-and-poll pattern. The poll
parameters are configurable:

```python
result = await client.lock(vin, poll_attempts=15, poll_interval=2.0)
```

When MQTT is enabled (default), pyBYD uses MQTT-first completion:

1. trigger command via HTTP,
2. wait briefly for MQTT `remoteControl` response,
3. fall back to HTTP polling if no MQTT result arrives.

This preserves reliability while reducing command-result latency when
MQTT is available.

On successful command completion, pyBYD applies an optimistic update to
state-store command-related fields (for example lock/window/climate state).
This allows integrations to reflect the desired target state immediately
while waiting for the next backend telemetry refresh.

MQTT `vehicleInfo` payloads are merged into the internal state store,
keeping read methods as up to date as possible between explicit API polls.

MQTT-related configuration:

- `mqtt_enabled` / `BYD_MQTT_ENABLED` (default: enabled)
- `mqtt_keepalive` / `BYD_MQTT_KEEPALIVE` (default: 120)
- `mqtt_timeout` / `BYD_MQTT_TIMEOUT` (default: 10.0)

### Breaking: `on_command_ack` callback contract

`BydClient(..., on_command_ack=...)` now delivers a single structured
`CommandAckEvent` object only.

```python
from pybyd import BydClient, BydConfig, CommandAckEvent

def on_command_ack(event: CommandAckEvent) -> None:
    # Deterministic correlation key
    print(event.vin, event.request_serial, event.is_correlated)
    # Diagnostics
    print(event.raw_uuid, event.result, event.success, event.timestamp)

client = BydClient(config, on_command_ack=on_command_ack)
```

Correlation is strict by `(vin, request_serial)` only. Events without
`request_serial` are diagnostics-only and must not be used for deterministic
command matching.

### Command lifecycle ownership (pending/match/expiry)

pyBYD owns the full remote-command ACK lifecycle registry:

- pending registration at trigger dispatch (`requestSerial` present)
- strict match by `(vin, request_serial)` only
- TTL-based expiry of unmatched pending entries
- diagnostics-only uncorrelated events (including serial-less ACKs)

Use `on_command_lifecycle` to consume lifecycle transitions:

```python
from pybyd import (
    BydClient,
    BydConfig,
    CommandLifecycleEvent,
    CommandLifecycleStatus,
)

def on_command_lifecycle(event: CommandLifecycleEvent) -> None:
    if event.status == CommandLifecycleStatus.MATCHED:
        print("matched", event.vin, event.request_serial, event.command)
    elif event.status == CommandLifecycleStatus.UNCORRELATED:
        print("uncorrelated", event.reason, event.request_serial)

client = BydClient(
    BydConfig.from_env(),
    on_command_lifecycle=on_command_lifecycle,
    command_ack_ttl_seconds=300.0,
)

diagnostics = client.get_command_ack_diagnostics()
print(diagnostics.pending, diagnostics.matched, diagnostics.expired, diagnostics.uncorrelated)
```

Lifecycle status values are:

- `registered`
- `matched`
- `expired`
- `uncorrelated`

`verify_command_access(vin)` must be called once during setup to verify
the control PIN and enable remote commands.  If verification fails,
commands remain disabled for the lifetime of the client.

## Error handling

```python
from pybyd import BydAuthenticationError, BydApiError, BydRemoteControlError

try:
    await client.login()
except BydAuthenticationError as e:
    print(f"Login failed: {e}")

try:
    await client.lock(vin)
except BydRemoteControlError as e:
    print(f"Command failed: {e}")
except BydApiError as e:
    print(f"API error: {e.code} at {e.endpoint}")
```

## Development

```bash
pip install -e ".[dev]"
pytest                    # run tests
ruff check .              # lint
mypy src/pybyd            # type check
```

## Scripts

Helper scripts live in `scripts/` and require `BYD_USERNAME` / `BYD_PASSWORD` env vars.

### `data_diff.py` — interactive change watcher

Pick a data category (realtime, HVAC, charging, …), then toggle
something on the vehicle or in the BYD app.  The script polls the API
and shows a colour-coded diff of exactly which fields changed — noisy
fields (timestamps, counters) are auto-calibrated away.

```bash
python scripts/data_diff.py            # interactive menu
python scripts/data_diff.py --raw      # also show unparsed API fields
python scripts/data_diff.py --vin X    # target a specific VIN
```

### `dump_all.py` — fetch & print every endpoint

```bash
python scripts/dump_all.py             # human-readable
python scripts/dump_all.py --json -o dump.json
python scripts/dump_all.py --skip-gps --skip-energy
```

### `generate_api_mapping_tables.py` — GitHub issue mapping tables

Polls data endpoints and builds two markdown tables per endpoint (Mapped and Unmapped) with:

- raw API key
- raw current value
- parsed value in pyBYD

Enum-mapped fields include the full enum domain in line-shifted rows
inside the same table cell, to make verification easy during mapping
collaboration.

```bash
python scripts/generate_api_mapping_tables.py
python scripts/generate_api_mapping_tables.py --vin X --output api-mapping-live.md
python scripts/generate_api_mapping_tables.py --skip-push
```

### `mqtt_probe.py` — passive MQTT watcher

Connects to the BYD MQTT broker, subscribes to your user topic, and
prints decrypted payloads in real time.

```bash
python scripts/mqtt_probe.py                        # watch indefinitely
python scripts/mqtt_probe.py --duration 600 --json  # 10 min, JSON output
```

## Credits

- [Niek/BYD-re](https://github.com/Niek/BYD-re) -- initial reverse
  engineering of the BYD app HTTP crypto path, Bangcle envelope codec,
  and Node.js reference client.
- [TA2k/ioBroker.byd](https://github.com/TA2k/ioBroker.byd) -- ioBroker
  adapter that provided additional endpoint definitions (energy
  consumption, remote control).

## License

MIT
