# MonkeyTester-Plugin-Server

The `esp32-server` device plugin for
MonkeyTester. It owns
the **`role: hub`** ESP32 boards plugged into its host — the project's "server"
boards — drives them over the MT control serial protocol, and runs the **fake
software server**: a software hub (HTTP API on :8092, registry, history, rule
language, mDNS `tshub-fake`) while no hub board is attached, so app tests don't
need a second board.

The **server firmware** is product code and lives in its own repo, not here; its `-test`
builds include MT control. Build it there and flash the `result/` into a board
with this plugin's `flash` action.

The `role: sensor` boards belong to the sibling
MonkeyTester-Plugin-Esp32
plugin (`esp32-sensor`, :8091), never to this one: a node that answers
`status` with the sensor role is probed, dropped and its console lock
released, so the other plugin adopts it; the shared role cache keeps this
plugin from touching such a node again. Both plugins split the boards through
per-node console locks and a role cache, so two plugin processes never open
the same serial console.

MonkeyTester reaches it only through the shared **Device Plugin API**
(`PLUGIN_API.md` in the MonkeyTester repo). Boards are driven over their serial
console with the **MT control protocol** (`docs/mt-control.md`), which the
firmware's `…-test` builds include; the server protocol is
`docs/server-protocol.md` in the server firmware.

```
MonkeyTester ──HTTP (plugin API)──► esp32_plugin.py :8093 (Linux or macOS)
                                     ├── esptool ───────────► flash
                                     ├── serial (MT control) ► ESP32 server boards
                                     └── fake server :8092 + mDNS tshub-fake (no server board attached)
```

## Install

| Dependency | Why | Required |
|---|---|---|
| Python 3.10 or newer | the plugin | yes |
| pyserial | the boards' serial consoles | yes |
| esptool | `flash`, and resetting native USB-Serial-JTAG boards | yes |
| zeroconf | the fake server's mDNS advertisement | yes |
| Membership of the `dialout` group | opening `/dev/ttyUSB*` / `/dev/ttyACM*` | on Linux |

```bash
git clone <repo-url> ~/Workspace/MonkeyTester-Esp32-Server
cd ~/Workspace/MonkeyTester-Esp32-Server
```

`~/Workspace/MonkeyTester-Esp32-Server` is the path the systemd unit in `packaging/` expects.

### With Nix (Linux or macOS)

Needs Nix with flakes enabled (`experimental-features = nix-command flakes` in
`~/.config/nix/nix.conf`). The flake brings Python, the Python packages and esptool, so
there is nothing else to install:

```bash
nix run .#tests            # unit tests against fake boards
nix run                    # the plugin on 127.0.0.1:8093 (loopback only)
nix develop                # a shell with python3 + the packages + esptool
```

### Without Nix

**Debian / Ubuntu:**

```bash
sudo apt-get install -y python3 python3-venv git
python3 -m venv .venv
.venv/bin/pip install pyserial esptool zeroconf
```

**macOS (Homebrew):**

```bash
brew install python git
python3 -m venv .venv
.venv/bin/pip install pyserial esptool zeroconf
```

The plugin finds esptool on `PATH`, else runs `<its python> -m esptool`, so the venv's copy
is enough.

### Serial-port access (Linux)

```bash
sudo usermod -aG dialout "$USER"   # then log out and back in
id -nG | grep -w dialout           # prints dialout when it worked
```

### Verify

```bash
# With Nix
nix run .#tests
nix develop -c esptool version

# Without Nix
.venv/bin/python -c "import serial, esptool, zeroconf"
.venv/bin/python -m unittest discover -s tests
```

The tests use a fake board and a fake esptool, so they need no hardware. Then start the
plugin (below) and check `curl -s localhost:8093/health`.

## Run it

Install the dependencies first (see [Install](#install)), then:

```bash
nix run                          # plugin on 127.0.0.1:8093 (fake server on 0.0.0.0:8092)
.venv/bin/python esp32_plugin.py # the same, without Nix
```

Check it with `curl localhost:8093/health`, `curl localhost:8093/devices` and
`curl localhost:8092/info.json`. The fake server's port must be reachable from
the LAN (phones and real sensors connect to it): open TCP 8092 and mDNS (UDP
5353) in the host firewall. Its settings:

| Variable | Default | |
|---|---|---|
| `ESP32_FAKE_HUB` | `auto` | `auto`: run while no `role: hub` board is attached; `on`: always (and `device=hub` is always the fake); `off`: never |
| `ESP32_FAKE_HUB_PORT` | `8092` | HTTP port |
| `ESP32_FAKE_HUB_BIND` | `0.0.0.0` | listen address (LAN on purpose: the sensor board connects over Wi-Fi) |
| `ESP32_FAKE_HUB_ADDR` | default-route IPv4 | the address advertised over mDNS and reported as the server's `ip` |

Every environment variable is listed at the top of `esp32_plugin.py`, including
`ESP32_LOCK_DIR` (default `/tmp/mt-esp32-locks`) and `ESP32_ROLE_CACHE`
(default `/tmp/mt-esp32-roles.json`) — the board-ownership files **shared**
with the esp32-sensor plugin: keep both plugins' settings of them in sync.

### Network access and the token

The plugin API listens on **127.0.0.1 only** by default (`PLUGIN_BIND`), which is
all the viewer needs when it runs on the same host (it reaches the plugin at
`http://127.0.0.1:8093`). The sibling esp32-sensor plugin has the same defaults.

To reach the plugin from another machine, bind it to a LAN address **and** set a
token, or it refuses to start (exit code 2, with the reason on stderr):

```bash
export PLUGIN_TOKEN=$(openssl rand -hex 32)    # at least 16 characters
PLUGIN_BIND=0.0.0.0 PLUGIN_TOKEN=$PLUGIN_TOKEN nix run
curl -s -H "X-Plugin-Token: $PLUGIN_TOKEN" http://<plugin-host>:8093/devices
```

With a token set, every request except `GET /health` needs the
`X-Plugin-Token` header (401 otherwise); on loopback the token is optional and,
when set, enforced the same way. The viewer keeps each plugin's token in
**Settings → Device Plugins** (MonkeyTester PR in progress; until it lands, the
viewer's `PLUGIN_TOKEN` / `MT_PLUGIN_TOKEN_<NAME>` environment). There is no
TLS: use a VPN or Tailscale to reach the plugin across untrusted networks.

The fake server (:8092) is the exception: it keeps listening on every interface
(`ESP32_FAKE_HUB_BIND`, default `0.0.0.0`) because the real sensor board and the
phone reach it over Wi-Fi, just like a real server board. It speaks only the hub
protocol (no token, like the firmware), keeps its state in memory and can't touch
files, serial ports or esptool; request bodies are capped at 4 KiB. See
[SECURITY.md](SECURITY.md).

`flash` only reads firmware from `ESP32_FIRMWARE_ROOT` (default: the working
directory), any extra directory listed in `ESP32_FIRMWARE_ROOTS`
(`:`-separated), the upload directory and `/nix/store`; other paths, `..` and
symlinks out of those roots are refused. Details and limits: [SECURITY.md](SECURITY.md).

To keep it running: `packaging/systemd/monkeytester-esp32-server.service`
(Linux, user unit) or `packaging/launchd/com.blackjackfruit.monkeytester-esp32-server.plist`
(macOS LaunchAgent). Run the esp32-sensor plugin's unit alongside; the split
happens by itself through the lock files and role cache.

### Start and stop

While it runs, the plugin keeps its boards' serial ports open. **Stop it before
you flash a board or open a serial monitor yourself** (esptool, screen, …), and
start it again when you are done, or CI runs cannot reach the boards.

**Linux (systemd user service):**

```bash
cp packaging/systemd/monkeytester-esp32-server.service ~/.config/systemd/user/
#   (edit WorkingDirectory if the checkout is not ~/Workspace/MonkeyTester-Esp32-Server)
systemctl --user daemon-reload
systemctl --user enable --now monkeytester-esp32-server

systemctl --user stop monkeytester-esp32-server      # frees the ports
systemctl --user start monkeytester-esp32-server
systemctl --user restart monkeytester-esp32-server
systemctl --user status monkeytester-esp32-server
journalctl --user -u monkeytester-esp32-server -f
```

**macOS (LaunchAgent):** replace `PYTHON` and `CHECKOUT_DIR` in the plist
(a python with pyserial, esptool and zeroconf), copy it to
`~/Library/LaunchAgents/`, and `launchctl load`/`unload` it.

### Pointing MonkeyTester at it

```bash
MT_PLUGINS=esp32-sensor=http://<plugin-host>:8091,esp32-server=http://<plugin-host>:8093
```

(`<plugin-host>` is `127.0.0.1` when the viewer runs on the same host, which is
the default setup; anything else needs `PLUGIN_BIND` + `PLUGIN_TOKEN` as above.)

and, in a test (server steps hit this plugin; sensor steps hit the other one):

```
PLUGIN: esp32-server factory-reset
PLUGIN: esp32-sensor factory-reset
PLUGIN: esp32-sensor ble-advertise seconds=0
# … the app provisions the sensor over BLE (BJF-<mac>); it reports to the server …
PLUGIN: esp32-server expect
  GET /devices -> sequence 17 18 19
END
VERIFY: The dashboard shows 17 °C
PLUGIN: esp32-server forget mac=7c:df:a1:a4:8f:44
VERIFY: The dashboard shows no sensors
```

## What it does

See [ACTIONS.md](ACTIONS.md) for the actions (`flash`, `reset`,
`factory-reset`, `expect`, `clear`, `forget`), the fake server and the roles.
In short:

- **Devices** are the `role: hub` boards (identified by MAC, learned from the
  firmware's `status` or read off a native USB-Serial-JTAG port's USB serial
  number) and, while it runs, the **fake server** (`tshub-fake`). `device=hub`
  or `server` (the default) resolves to the one hub board, or the fake when none
  is attached; `device=sensor` is answered with `wrong_plugin`.
- **flash** takes a prebuilt ESP-IDF build directory — e.g. the `result/` of
  `nix build .#firmware-c3-release-test` in the server firmware.
- **expect** is a relay on a board (the rule language belongs to the firmware,
  its `docs/server-protocol.md` + `docs/mt-control.md`); on the fake
  server the plugin applies the hub rule language itself.
- **forget** makes the server (real or fake) drop one sensor from its registry
  and history, like `DELETE /devices/<mac>`.
- **factory-reset** erases the registry, history and rules and reboots.

Job outcomes follow the contract: `passed`; `failed` when the board rejected a
rule or didn't answer over MT control; `timeout`; `error` when the plugin
itself couldn't do it (bad firmware dir, esptool failure, port gone).

## Layout

| File | What |
|---|---|
| `plugin_server.py` | The generic plugin API server: jobs, long-poll waits, events, uploads. No ESP32 code. |
| `security.py` | Bind/token policy and request-input validators (MAC, serial port, job/upload ids, contained paths); identical in both ESP32 plugin repos. See [SECURITY.md](SECURITY.md). |
| `esp32_plugin.py` | Board discovery, role ownership, actions, flashing, the fake server, `main()` |
| `fake_hub.py` | The fake server: hub HTTP API, registry/history, rule language, mDNS |
| `board.py` | One board on one serial port: its transport (`port_kind`: UART bridge or native USB-Serial-JTAG), reader thread, MT control, reset |
| `tests/` | Contract and action tests with fake boards (`fake_board.py`) and the fake server; `test_hub.py` covers the server roles, the fake server and the split ownership |
