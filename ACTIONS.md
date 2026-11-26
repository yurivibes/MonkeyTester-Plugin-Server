# ESP32-SERVER plugin actions

Every action runs as a job (`POST /jobs`, then `GET /jobs/<id>?wait=…`; see
`PLUGIN_API.md` in MonkeyTester). `GET /actions` returns the same list with full
parameter schemas.

## Devices and roles

This plugin owns the **`role: hub`** boards — the project calls them "server"
boards (`docs/server-protocol.md` in the server firmware; the firmware's status field
says `hub`, which stays the wire value and `server` is only a `device=` alias).
Sensor boards belong to the ESP32-SENSOR plugin: a node that answers `status`
with `role: sensor` (or one that never answers MT control at all, which counts
as a sensor) is probed, dropped and its console lock released so ESP32-SENSOR
adopts it; the shared role cache keeps the next scan from touching it.

`device=` in a test step (the job's `device`) is one of:

| Value | Resolves to |
|---|---|
| omitted, or `hub`/`server` | The one attached hub board; when none is attached, the plugin's **fake server** (see below), depending on `ESP32_FAKE_HUB`. Fails (`bad_request`, 400) when there are several: then pass the MAC |
| `sensor` | `wrong_plugin` (404): sensor boards belong to the ESP32-SENSOR plugin |
| a MAC, or a serial port | That board; `tshub-fake` (or the fake server's MAC `02:00:00:fa:4e:01`) is the fake server |

`GET /devices` lists every owned board with `details.role`, `details.port` (the
serial node it is on) and `details.port_kind` (how it is reached: `uart-bridge`
through a CP2102N UART adapter, or `usb-serial-jtag` over the chip's own
USB-Serial-JTAG interface), plus the fake server entry (`details.fake: true`,
`details.url`, `details.mdns`) while it runs. Hub entries carry the firmware's
`sensors` and `time_synced` status fields.

## The two transports

A board is reached over one of two serial transports (`board.py:port_kind`,
reported as `details.port_kind`):

| Port kind | Node | What it gives the plugin |
|---|---|---|
| `uart-bridge` | `/dev/ttyUSB*` (macOS `cu.usbserial*`): the CP2102N adapter | a baud rate (`ESP32_BAUD`, `ESP32_FLASH_BAUD`) and an EN/RTS pin to pulse |
| `usb-serial-jtag` | `/dev/ttyACM*` (macOS `cu.usbmodem*`): the chip's own interface | nothing to set (CDC-ACM ignores the baud rate and has no DTR/RTS, so the plugin touches neither), no pin to reset through, and a USB serial number that **is the board's MAC** |

A native board is identified at discovery, before it answers MT control. It has
no reset button on the wire: `reset` runs esptool on that port, which ends with
its own hard reset. esptool may make the kernel re-create the interface with
another index, so the plugin re-enumerates the nodes after every esptool run
and the board follows its MAC to the new node — `details.port` changes, its id
and events don't. The `baud` parameter and `ESP32_BAUD`/`ESP32_FLASH_BAUD` mean
nothing there.

## The fake server

When no hub board is attached, the plugin runs the server in software so app
tests don't need a board. It implements the server firmware's `docs/server-protocol.md`
sections 1-4 and the server rule language from `docs/mt-control.md`, on the
plugin's host:

- **HTTP** on `0.0.0.0:8092` (`ESP32_FAKE_HUB_PORT`, `ESP32_FAKE_HUB_BIND`; LAN on purpose, the sensor board connects over Wi-Fi; bodies over 4 KiB are a 413, see SECURITY.md):
  `POST /report`, `GET /info.json`, `GET /devices`, `GET /devices/<mac>`,
  `GET /devices/<mac>/history`, `DELETE /devices/<mac>`. Phones and real sensors
  on the LAN must be able to reach this port.
- **mDNS:** `_tsserver._tcp`, instance and hostname `tshub-fake`, TXT `proto=1`,
  `mac=02:00:00:fa:4e:01`, `fw=<plugin version>`, on one IPv4 address: the
  default-route interface's, or `ESP32_FAKE_HUB_ADDR`. Needs python `zeroconf`
  (in the flake; `pip install zeroconf` otherwise). If advertising fails, HTTP
  keeps working and `details.mdns` in `/devices` says why.
- Registry of 16 sensors keyed by MAC (evicts the one offline longest; 507 when
  all 16 are online), per-minute history (180 points, last good reading of each
  minute), `online` while the last report is younger than
  `max(3 × interval_s, 15 s)`, epoch-ms times from the host clock
  (`time_synced` is always true). Everything lives in memory: restarting the
  plugin empties it.

`ESP32_FAKE_HUB` chooses when `device=hub` uses it:

| Value | Fake server |
|---|---|
| `auto` (default) | Runs while no `role: hub` board is attached. When a hub board appears it stops serving and stops advertising, so sensors and the app move to the real server; it starts again when the board goes away |
| `on` | Always runs, and `device=hub` is always the fake (a hub board is still reachable by its MAC) |
| `off` | Never runs; `device=hub` needs a hub board |

Tests don't change between the fake server and a real hub board.

## Actions

| Action | Params | Passes when | Default timeout |
|---|---|---|---|
| `flash` | `firmware_dir` (path, required — e.g. `result/` of `nix build .#firmware-c3-release-test` in the server firmware; must be under `ESP32_FIRMWARE_ROOT`/`ESP32_FIRMWARE_ROOTS`/`/nix/store`, or `upload:<id>`, anything else is a 400, see SECURITY.md), `erase` (bool, false), `wait_ready` (bool, true), `baud` (int; ignored on a native port) | esptool succeeds and, with `wait_ready`, the board prints `@mt - ready` (or answers `status`) after reboot | 180 s |
| `reset` | `wait_ready` (bool, true) | The board reboots — EN is pulsed through the adapter's RTS pin on a `uart-bridge` port, esptool's own hard reset on a native `usb-serial-jtag` one — and prints `@mt - ready`. A reboot drops every rule | 30 s |
| `factory-reset` | — | The board forgets its build-time network state, reboots, and comes back ready with no rules (the fake server: registry, history and rules erased) | 30 s |
| `expect` | `script` (string, required; the test's inline block) | The device accepted every rule line | 30 s |
| `clear` | — | The device dropped every rule | 10 s |
| `forget` | `mac` (string, required) | The server forgot that sensor (registry and history), like `DELETE /devices/<mac>` | 10 s |

What each action does per device:

| Action | hub board | fake server |
|---|---|---|
| `flash`, `reset` | yes | `unsupported` (error) — software, no firmware/reset line |
| `factory-reset` | erases state, reboots | clears registry, history and rules |
| `expect` | relays `rule` lines | applies the server rule language itself |
| `clear` | relays `clear` | drops every rule |
| `forget` | relays `forget <mac>` | forgets the sensor; `device_error` if unknown |

The provisioning actions (`ble-advertise`, `broadcast-ssid`) live in the
ESP32-SENSOR plugin; asking for one here fails with `unknown_action`.

## Starting a test

Start every app test from factory settings — server side here, sensor side at
the other plugin — then let the app find the sensor:

```
ESP32-SERVER: factory-reset
ESP32-SENSOR: factory-reset
ESP32-SENSOR: ble-advertise seconds=0
# …the app finds BLE device BJF-<mac> and provisions the sensor; the sensor
# reports to the server (the fake one when no hub board is attached)…
ESP32-SERVER: expect
  GET /devices -> sequence 17 18 19
END
```

## `expect`

The test's inline block states **only what changes** in how the device answers
the app. Everything not mentioned stays as it was. On a hub board the plugin
doesn't interpret the rules: blank and `#` lines are dropped and each line goes
out as `rule <line>` over the MT control protocol; the rule language belongs to
the firmware (its `docs/server-protocol.md`, `docs/mt-control.md`: HTTP
responses with a status and body, `hang`, `delay`, `sequence`, `normal` to undo
one rule). On the fake server the plugin applies the same rule language itself
(`<METHOD> <path> -> <status> [<body>]`, `hang`, `delay <ms> <status> [<body>]`,
`sequence <t1> …`, `normal`; `{device}` `{mac}` `{ip}` `{ts_ms}` substituted;
`sequence` replaces every `temperature_c` with the next value; `hang` holds the
request up to 120 s; a rule for `POST /report` replaces the real handler).

Sensor directives (`report on|off`, `temp …`, `wifi`/`mqtt`) belong to sensor
boards at the ESP32-SENSOR plugin; a hub board rejects them.

Rules stay on the board until `clear`, a `reset`, or a reflash. If the device
rejects a line, the job fails with `rule_rejected`; `result.line` says which
line and `result.applied` how many lines before it took effect. Lines after it
aren't sent.

## Failure codes

`error.code` on a job that didn't pass:

| Code | State | Meaning |
|---|---|---|
| `rule_rejected` | failed | The firmware rejected a rule line (`result.line`, `result.error`) |
| `no_reply` | failed | No MT reply in time: hung firmware or a build without MT control |
| `device_error` | failed | The firmware answered `err` to `clear`/`forget`, or the fake server doesn't know the MAC given to `forget` |
| `unknown_device` | (HTTP 404 on `POST /jobs`) | `device=` matched nothing: no hub board and `ESP32_FAKE_HUB=off`, or an unknown MAC/port |
| `unknown_action` | (HTTP 404 on `POST /jobs`) | Sensor-side actions (`ble-advertise`, `broadcast-ssid`) live in the ESP32-SENSOR plugin |
| `wrong_plugin` | (HTTP 404 on `POST /jobs`) | `device=sensor`: belongs to the ESP32-SENSOR plugin |
| `bad_request` | error | `script` has no rule lines; `forget` got a malformed `mac` |
| `unsupported` | error | `flash` or `reset` on the fake server |
| `bad_firmware` | error | `firmware_dir` has no usable `flasher_args.json` or images |
| `flash_failed`, `reset_failed`, `tool_missing` | error | esptool failed or isn't installed |
| `port_closed`, `port_error` | error | The serial port went away |
| `timeout` | timeout | The job's `timeout_ms` ran out |
