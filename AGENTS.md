# MonkeyTester-Esp32-Server — Agent Guide

## Purpose
The `esp32-server` device plugin for MonkeyTester: owns the `role: hub`
("server") ESP32 boards on its host, flashes prebuilt firmware, drives them
over the VT control serial protocol, and runs the fake software server (:8092,
mDNS `tshub-fake`) while no hub board is attached. The server FIRMWARE is
product code in Esp32-Server, not here. The sibling esp32-sensor plugin
owns the sensor boards. See README.md.

## Setup (do this first)
Install the dependencies with the steps in README.md → **Install** before running or testing
anything; that section is the single source for them. In short:
- **Nix available** (`command -v nix`): nothing to install. `nix run .#tests` runs the tests,
  `nix run` starts the plugin on :8093, `nix develop` gives a shell with Python, pyserial, zeroconf
  and esptool.
- **No Nix:** Python 3.10+, then a `.venv` with `pip install pyserial esptool zeroconf`
  (README.md → Install → Without Nix).
- On Linux, opening real boards needs the `dialout` group; the tests don't (they use
  `tests/fake_board.py` and `tests/fake_esptool.py`).
- Don't start the plugin on this host to try a change while the live
  `monkeytester-esp32-server` service runs: it holds the boards' serial ports. The tests need no hardware.
- A new dependency goes into README.md → Install and `flake.nix` in the same PR. If a step in
  README.md → Install doesn't work as written, fix it in the same PR.

## Critical Rules
- **The plugin API is a contract.** `plugin_server.py` implements
  `PLUGIN_API.md` from the MonkeyTester repo. Don't add or change endpoints,
  job states or fields here without changing that document first (and bumping
  `contract_version` for breaking changes).
- **The device protocol is owned by the firmware.** Board commands and the rule
  language follow `docs/server-protocol.md` and `docs/vt-control.md` in Esp32-Server.
  For boards, `expect` relays rule lines without interpreting them; don't add
  parsing or validation of board rules in the plugin.
- **Sensor boards are never ours.** A node that answers `status` with `role: sensor`
  (or never answers VT control — that counts as a sensor) is probed, dropped and
  its console lock released so esp32-sensor adopts it. Hub actions (`forget`)
  and provisioning stay split: `ble-advertise`/`broadcast-ssid` belong to the
  sensor plugin — don't move them back. The lock dir and role cache defaults
  (`/tmp/vt-esp32-locks`, `/tmp/vt-esp32-roles.json`) must match esp32-sensor's
  exactly: they are the cross-process protocol.
- **The fake server (`fake_hub.py`) is the one protocol exception:** it
  implements server-protocol.md sections 1-4 and the server rule language itself.
  Change it only together with that spec, and keep it behaving like the hub
  firmware (tests must not change between the fake and a real hub).
- **Two transports, one protocol.** A board is either on a CP2102N UART bridge
  (`uart-bridge`, `/dev/ttyUSB*`) or on its own USB-Serial-JTAG interface
  (`usb-serial-jtag`, `/dev/ttyACM*`): native means no baud rate, no DTR/RTS
  (pyserial raises), esptool resets it, and the USB serial number is the MAC.
  `board.py:port_kind` decides; ACTIONS.md documents it.
- **Must run on Linux and macOS** (the firmware builds are Linux-only; the plugin
  is both). Port globs are per-OS in `DEFAULT_GLOBS`. Python stdlib + pyserial +
  esptool, plus zeroconf for the fake server's mDNS (imported lazily; tests mock it).
- **No test logic.** The plugin doesn't know about apps or vision models; it only
  makes boards do things and reports whether they did.
- Keep ACTIONS.md in step with `_build_actions()`.
- **Keep the user informed of user-relevant changes.** A change is user-relevant
  when it changes what a person running the plugin by hand does or sees: how to
  install, start, stop or restart it (systemd unit, LaunchAgent, `nix run`),
  ports (8093 here, fake server 8092, mDNS `tshub-fake`), environment variables, board discovery and serial-port
  handling, the curl/HTTP commands in README.md, test step syntax (`PLUGIN:
  esp32-server …`), or required packages/groups (`dialout`). For every such
  change:
  - update README.md (and ACTIONS.md for actions) **in the same PR**; the README
    commands must work as written;
  - add a **"For the user"** section to the PR description saying in plain
    words what changed for them and what, if anything, they must do;
  - call it out in the report to the user, not only in the PR.
  If a change is not user-relevant, say "For the user: no change" in the PR.
- Work in a git worktree on a feature branch and open a PR; never commit to master.

## Tests
`nix run .#tests` (or `python3 -m unittest discover -s tests` with pyserial
installed). They use `tests/fake_board.py` and `tests/fake_esptool.py`; the
fakes cover both transports (`FakeSerial` carries a port kind, a CDC-ACM one
raises on DTR/RTS like pyserial; `UsjNodes` moves the fake `/dev/ttyACM*` index
after every fake-esptool run). Test harnesses pin `lock_dir=""`/`role_cache=""`
by default so the shared /tmp files are never touched; the ownership tests use
a temp cache file with the real helpers. The fake-server tests bind 127.0.0.1
on random ports and never touch real mDNS.
