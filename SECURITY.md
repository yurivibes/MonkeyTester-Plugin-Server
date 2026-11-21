# Security — MonkeyTester-Esp32-Server

This is the `esp32-server` device plugin for MonkeyTester. This page says what it
protects, how, and what it does not. It makes **no "zero vulnerabilities"
claim**: it closes the known holes and keeps the attack surface small for its
intended setting.

## Threat model

- **Setting:** a home lab LAN (192.168.125.0/24), one user, the plugin and the
  MonkeyTester viewer on the same Linux host. The viewer reaches the plugin at
  `http://127.0.0.1:8093`.
- **What an attacker on the LAN must not be able to do:** run plugin actions
  (flash arbitrary firmware, reset or reprogram boards, drive their serial
  consoles), read or write files on the host through the API, run commands, or
  learn secrets.
- **Out of scope:** other local users on the same host (see the shared `/tmp`
  note below), physical access to the boards, and attackers who already have the
  user's shell.

## Network exposure

| Listener | Port | Default bind | Auth |
|---|---|---|---|
| Plugin API | :8093 | `127.0.0.1` | `X-Plugin-Token` (mandatory off loopback) |
| Fake hub (hub protocol) | :8092 | `0.0.0.0` (by design) | none, like the firmware |

**The defaults are identical in MonkeyTester-Esp32-Sensor and
MonkeyTester-Esp32-Server** (same `PLUGIN_BIND` default, same token rule, same
limits; `security.py` and `plugin_server.py` are byte-identical in both repos).
The sibling plugin is MonkeyTester-Esp32-Sensor (`esp32-sensor`, :8091).

## Token setup

- `PLUGIN_BIND` defaults to `127.0.0.1`. Loopback (`127.0.0.0/8`, `::1`,
  `localhost`): the token is optional.
- Any other bind (a LAN address, `0.0.0.0`, `::`): `PLUGIN_TOKEN` is
  **mandatory** and must be at least 16 characters. Without it the plugin prints
  the reason to stderr and **exits with code 2** before it opens any board.
- Generate one with `openssl rand -hex 32`. Put it in a systemd drop-in
  (`systemctl --user edit monkeytester-esp32-server`: `Environment=PLUGIN_BIND=…` and
  `Environment=PLUGIN_TOKEN=…`) or the LaunchAgent's `EnvironmentVariables`.
- When a token is set (on any bind), every request except `GET /health` needs
  the `X-Plugin-Token` header; it is compared with `hmac.compare_digest`
  (constant time). Wrong or missing → 401.
- `GET /health` stays unauthenticated (the viewer's liveness check). It returns
  the contract, plugin name/version, device count, capabilities and the device
  error string; the host name only to authenticated callers. It never returns
  the token, the environment or configuration.
- The viewer stores each plugin's token in **Settings → Device Plugins**
  (MonkeyTester PR in progress; until it lands the viewer still reads
  `PLUGIN_TOKEN` / `VT_PLUGIN_TOKEN_<NAME>` from its environment). The token is
  stored in **plaintext at rest** on both sides (env/unit drop-in here, a 0600
  JSON file in the viewer).

## Input hardening

- **Request size:** JSON bodies are capped at 1 MiB, `POST /uploads` at
  64 MiB → 413. A body needs a `Content-Length` (chunked → 411); a bad length
  → 400. Malformed JSON, bad UTF-8 or absurd nesting → 400. `timeout_ms` is
  capped at one hour. Each connection has a 30 s socket timeout.
- **No tracebacks to clients:** an internal error answers
  `internal plugin error; see the plugin's log`; the traceback goes to the
  plugin's stderr/journal only (also for crashing jobs, whose log and error
  carry only the exception type).
- **Identifiers:** job ids must look like `j-<12 hex>`, upload refs like
  `upload:<12 hex>`, MACs `aa:bb:cc:dd:ee:ff`; a `device=` value must be a role
  alias, a MAC, a serial port path (`/dev/serial/by-id/*`, `/dev/ttyUSB<n>`,
  `/dev/ttyACM<n>`, macOS `/dev/cu.*`) or the exact port of a board the plugin
  already has — anything else is answered like an unknown board (404).
- **Firmware paths:** `firmware_dir` is resolved with `realpath` and must stay
  (by `commonpath`) under `ESP32_FIRMWARE_ROOT` (default: the working directory),
  an extra root in `ESP32_FIRMWARE_ROOTS`, the upload directory or `/nix/store`
  (a Nix `result` link resolves there; the store is world-readable anyway). `..`
  components, NUL bytes and symlinks leading out of those roots → 400.
- **Uploads:** zip entries with absolute paths, `..`, or symlink type → 400;
  each target is re-checked with `realpath` + `commonpath`; at most 2000 entries
  and 256 MiB unpacked (counted while inflating, not trusted from the headers)
  → 413. Only `.bin` and `.json` files are written; other entries are skipped.
  The upload root is created `0700` and must be a real directory owned by the
  plugin's user. Uploads expire after an hour.
- **Subprocesses:** esptool is the only one, always started with an argv list
  (never a shell). Everything `flasher_args.json` (possibly uploaded) puts on
  its command line is allow-listed: chip (`esp32*`), reset modes, the
  `write_flash` options (`--flash_mode/size/freq`, compression), flash
  addresses, and image paths (relative `.bin` files that resolve inside the
  firmware dir). A port starting with `-` is refused. `ESPTOOL` itself comes
  from the operator's environment and is trusted.
- **bandit:** `nix run nixpkgs#python3Packages.bandit -- -r . -x ./tests`
  reports no medium/high findings; the remaining `# nosec` markers are
  justified inline (argv-only subprocess calls, the shared `/tmp` protocol
  paths, the fake hub LAN bind).

## The fake hub (:8092) stays on the LAN

The fake server is the one listener in this repo that keeps a LAN bind
(`ESP32_FAKE_HUB_BIND`, default `0.0.0.0`). That is deliberate: the real sensor
board (and the phone running the app under test) reach it over Wi-Fi, exactly
like a real server board, so binding it to loopback would break every test that
uses it. Like the real firmware's hub API it has **no authentication** (the
sensor firmware has no way to send a token).

What limits the exposure:

- It is a device-protocol endpoint only: an in-memory registry, history and rule
  table. Nothing it serves can reach a file, a serial port, esptool or any other
  subprocess (`tests/test_hub.py` checks the module imports no `os`/`subprocess`/
  `shutil` and calls no `open`).
- Request bodies over 4 KiB are a 413, a bad `Content-Length` a 400, chunked
  bodies a 411, paths over 256 characters a 414; the connection is closed after
  any of those. `POST /report` keeps its own 512-byte cap and schema check
  (MAC format, types). Every connection has a 10 s socket timeout.
- Rule-driven `hang`/`delay` responses are bounded (120 s / 60 s) and released
  when a rule is cleared.

What someone on the LAN can still do to it: register fake sensors (up to 16),
read the registry, `DELETE /devices/<mac>`, or hold connections open — the same
things they could do to a real server board. Turn it off with
`ESP32_FAKE_HUB=off` when no test needs it, or bind it to the host's Wi-Fi
address only with `ESP32_FAKE_HUB_BIND`.

## Shared `/tmp` files

The board-ownership protocol with the sibling plugin uses fixed paths that must
match in both repos: `/tmp/vt-esp32-locks` (lock dir, created `0700`) and
`/tmp/vt-esp32-roles.json` (role cache). Lock files are opened with
`O_NOFOLLOW` (`0600`); the cache is written to a private `mkstemp` file and
renamed over the target, so a pre-planted symlink is never followed. Another
local user could still pre-create those paths and disturb board ownership
(denial of service, not code execution); on a shared machine point
`ESP32_LOCK_DIR` / `ESP32_ROLE_CACHE` (in **both** plugins) at a private
directory.

## What is NOT covered

- **No TLS.** Tokens and traffic cross the LAN in clear text when the plugin is
  bound off loopback. For remote access use a VPN or Tailscale, not a
  port-forward.
- **Plaintext secrets at rest:** the token lives in the unit/plist environment
  here and in the viewer's settings file.
- **Tier 3 items** (not done): systemd sandboxing of the unit
  (`ProtectSystem=`, `PrivateTmp=` would break the shared `/tmp` protocol and
  need a coordinated change in both repos), TLS termination, rate limiting and
  connection caps (many open SSE or long-poll connections can tie up threads).
- The serial protocol to the boards and the boards themselves are trusted.

## Reporting a problem

This is a single-user home-lab project. Open an issue on the Gitea repo
(`yurivibes/MonkeyTester-Esp32-Server`) or tell the maintainer directly; don't
post working tokens in the issue.
