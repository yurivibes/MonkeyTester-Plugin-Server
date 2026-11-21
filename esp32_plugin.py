#!/usr/bin/env python3
"""MonkeyTester-Plugin-Esp32-Server: the `esp32-server` device plugin for MonkeyTester.

Owns the `role: hub` ESP32 boards attached to this host — the project's "server"
boards — and runs the fake software server: a software hub (HTTP API on :8092,
registry, history, rule language, mDNS `tshub-fake`) while no hub board is
attached. The `role: sensor` boards belong to the sibling esp32-sensor plugin
(MonkeyTester-Plugin-Esp32-Sensor), never to this one: a node that answers `status`
with the sensor role is probed, dropped and its console lock released, so the
other plugin adopts it; the shared role cache lets this plugin skip such a node
without touching it again. Actions are listed in ACTIONS.md.

Boards are reached over one of two transports (board.py:port_kind): the CP2102N
UART bridge (`uart-bridge`, /dev/ttyUSB*) — a real baud rate and an EN/RTS pin —
or the chip's own USB-Serial-JTAG interface (`usb-serial-jtag`, /dev/ttyACM*):
CDC-ACM with no baud rate, no RTS pin (esptool resets it), and a USB serial
number that is the board's MAC, which identifies it before it answers VT
control. `GET /devices` reports `details.port_kind`.

Environment:
  PLUGIN_BIND        listen address           (default 127.0.0.1, loopback only)
  PLUGIN_PORT        listen port              (default 8093)
  PLUGIN_TOKEN       required as X-Plugin-Token on every request but GET /health;
                     MANDATORY (>= 16 chars) when PLUGIN_BIND is not loopback:
                     the plugin refuses to start otherwise (SECURITY.md)
  ESP32_PORTS        comma-separated serial ports or globs to use
                     (default: USB-UART ports for this OS, see DEFAULT_GLOBS)
  ESP32_BAUD         console baud rate        (default 115200)
  ESP32_FLASH_BAUD   esptool baud rate        (default 460800)
                     (both are meaningless on a board's native USB-Serial-JTAG
                     port — /dev/ttyACM*, CDC-ACM — where nothing is set)
  ESPTOOL            esptool command          (default: `esptool` on PATH, else
                     "<this python> -m esptool")
  ESP32_FIRMWARE_ROOT  relative firmware_dir values resolve against this
                     (default: the current directory)
  ESP32_FIRMWARE_ROOTS extra directories (os.pathsep-separated) a firmware_dir
                     may point into; anything outside ESP32_FIRMWARE_ROOT, these,
                     the upload dir and /nix/store is refused
  ESP32_READY_TIMEOUT_S  how long to wait for `@vt - ready` after a reset (default 20)
  PLUGIN_UPLOAD_DIR  where POST /uploads unpacks zips (default: <tmp>/vt-plugin-uploads)
  ESP32_FAKE_HUB     auto | on | off: when `device=hub` uses the software fake hub
                     (default auto: only while no `role: hub` board is attached)
  ESP32_FAKE_HUB_PORT  fake hub HTTP port   (default 8092)
  ESP32_FAKE_HUB_BIND  fake hub listen address (default 0.0.0.0: the real
                     sensor board reaches it over Wi-Fi; it is a device-protocol
                     endpoint with no file, flash or subprocess access)
  ESP32_FAKE_HUB_ADDR  IPv4 the fake hub advertises over mDNS and reports as its
                     `ip` (default: the address of the default-route interface)
  ESP32_LOCK_DIR     per-node console-lock files shared with the esp32-sensor
                     plugin (default /tmp/vt-esp32-locks)
  ESP32_ROLE_CACHE   JSON {board key -> role} shared with the esp32-sensor
                     plugin (default /tmp/vt-esp32-roles.json)
"""

from __future__ import annotations

import glob
import errno
import fcntl
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess  # nosec B404 - argv lists only (esptool), never a shell
import sys
import tempfile
import threading
import time

import plugin_server as ps
import security
from board import PORT_USB_SERIAL_JTAG, PORT_UART_BRIDGE, Board, port_kind
from fake_hub import FAKE_HUB_DEFAULT_BIND, FAKE_HUB_ID, MAC_RE, FakeHub
from plugin_server import (Action, ActionError, ActionFailed, HttpError, JobContext,
                           Param, Plugin, PluginServer, Uploads)

VERSION = "0.1.0"

DEFAULT_GLOBS = {
    "linux": ["/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"],
    "darwin": ["/dev/cu.usbserial-*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*",
               "/dev/cu.usbmodem*"],
}
RESCAN_S = 5.0
READY_TIMEOUT_S = float(os.environ.get("ESP32_READY_TIMEOUT_S", "20"))
FAKE_HUB_MODES = ("auto", "on", "off")
IDENTIFY_WAIT_S = 3.0
# A native USB-Serial-JTAG node vanishes and comes back after a chip reset.
USJ_REAPPEAR_S = float(os.environ.get("ESP32_USJ_REAPPEAR_S", "10"))
PLUGIN_NAME = "esp32-server"
PLUGIN_DEFAULT_PORT = 8093
# The board roles this plugin owns. The firmware's status field says `hub`,
# which stays the wire value; `server` is a `device=` alias. Sensor boards
# belong to the sibling esp32-sensor plugin, never to this one.
OWNED_BOARD_ROLES = ("hub", "server")
OTHER_BOARD_ROLES = ("sensor",)
# Fixed /tmp paths on purpose: they are the cross-process protocol with the
# sibling plugin and must be identical in both repos. Files are opened without
# following symlinks (locks) or replaced atomically from a private temp file
# (role cache); see SECURITY.md for the shared-/tmp caveat.
LOCK_DIR_DEFAULT = "/tmp/vt-esp32-locks"  # nosec B108 - shared protocol path, see above
ROLE_CACHE_DEFAULT = "/tmp/vt-esp32-roles.json"  # nosec B108 - shared protocol path
# After a missed discovery probe: how long to wait for the board's
# `@vt - ready` line (a port open resets a CP2102N board; it boots in <1 s).
IDENTIFY_READY_WAIT_S = 3.0
_MAC_IN_NAME = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _lock_name(port: str) -> str:
    """Lock file for a serial node, named after its real device path."""
    return re.sub(r"[^A-Za-z0-9_.+-]", "-", os.path.realpath(port))


def try_lock_port(lock_dir: str, port: str):
    """Try to take the shared lock for `port`.

    fd (locked by this process; the OS drops it if the process exits),
    None (the other plugin holds it, skip this node until the next rescan)
    or False (locking unavailable — e.g. a fake port: assume this host
    is the only plugin and keep the pre-split behaviour).
    """
    try:
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(lock_dir, _lock_name(port)),
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # exclusive, non-blocking
        return fd
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None  # the other plugin owns this console
        return False


def lock_cache_read(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def lock_cache_write(path: str, key: str, value: str) -> None:
    try:
        data = lock_cache_read(path)
        data[key] = value
        # A private, uniquely named temp file (O_EXCL, never a pre-planted
        # symlink), then an atomic rename over the cache.
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                                   dir=os.path.dirname(path) or ".")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


def usb_mac(port: str) -> str | None:
    """The MAC a serial node's USB metadata reports, if it reports one.

    An ESP32's native USB-Serial-JTAG interface puts the board's MAC in its USB
    serial number (uppercase colon form, e.g. `AC:27:6E:FA:BB:5C`), and udev puts
    that serial number in the `/dev/serial/by-id/usb-…` name of the node — so the
    board is identified at discovery, before it answers VT control. Other
    interfaces (the CP2102N bridge: its own serial, `68a5…`) don't, and those
    boards keep being identified by the firmware's `status` as before."""
    real = os.path.realpath(port)
    names = [os.path.basename(port), os.path.basename(real)]
    if not names[1].startswith("usb-"):  # a plain node: look for its by-id link
        for link in glob.glob("/dev/serial/by-id/*"):
            if os.path.realpath(link) == real:
                names.append(os.path.basename(link))
                break
    for name in names:
        m = _MAC_IN_NAME.search(name)
        if m and MAC_RE.match(m.group(0).lower()):
            return m.group(0).lower()
    return None


# ───────────────────────────── flashing ─────────────────────────────


def find_flasher_args(firmware_dir: str) -> tuple[str, dict]:
    """Locate flasher_args.json in an ESP-IDF build dir (or a Nix `result/`)."""
    for cand in (firmware_dir, os.path.join(firmware_dir, "build")):
        p = os.path.join(cand, "flasher_args.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    return cand, json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise ActionError(f"can't read {p}: {e}", code="bad_firmware") from None
    raise ActionError(f"no flasher_args.json in {firmware_dir} or {firmware_dir}/build "
                      "(pass an ESP-IDF build directory)", code="bad_firmware")


def esptool_command() -> list:
    cmd = os.environ.get("ESPTOOL")
    if cmd:
        return shlex.split(cmd)
    for exe in ("esptool", "esptool.py"):
        found = shutil.which(exe)
        if found:
            return [found]
    return [sys.executable, "-m", "esptool"]


# flasher_args.json comes from the firmware dir (possibly an upload), so every
# value it puts on esptool's command line is checked against these.
CHIP_RE = re.compile(r"^esp32[a-z0-9]*$")
ESPTOOL_RESET_MODES = frozenset({
    "default_reset", "default-reset", "no_reset", "no-reset", "no_reset_no_sync",
    "no-reset-no-sync", "usb_reset", "usb-reset", "hard_reset", "hard-reset",
    "soft_reset", "soft-reset", "watchdog_reset", "watchdog-reset"})
WRITE_FLASH_OPTIONS = frozenset({
    "--flash_mode", "--flash-mode", "--flash_size", "--flash-size", "--flash_freq",
    "--flash-freq", "--compress", "-z", "--no-compress", "-u"})
WRITE_FLASH_VALUE_RE = re.compile(r"^[A-Za-z0-9_.]{1,32}$")
FLASH_ADDR_RE = re.compile(r"^(0x[0-9a-fA-F]{1,8}|[0-9]{1,10})$")


def _bad_firmware(msg: str) -> ActionError:
    return ActionError(f"flasher_args.json: {msg}", code="bad_firmware")


def _checked_write_flash_args(values) -> list:
    if not isinstance(values, list):
        raise _bad_firmware("write_flash_args must be a list")
    out = []
    for v in values:
        if not isinstance(v, str):
            raise _bad_firmware(f"write_flash_args entry {v!r} is not a string")
        if v.startswith("-"):
            if v not in WRITE_FLASH_OPTIONS:
                raise _bad_firmware(f"write_flash option {v!r} is not allowed")
        elif not WRITE_FLASH_VALUE_RE.match(v):
            raise _bad_firmware(f"write_flash value {v!r} is not allowed")
        out.append(v)
    return out


def _checked_image(build_dir: str, addr, rel) -> tuple[str, str]:
    if not isinstance(addr, str) or not FLASH_ADDR_RE.match(addr):
        raise _bad_firmware(f"bad flash address {addr!r}")
    if (not isinstance(rel, str) or not rel or "\0" in rel or os.path.isabs(rel)
            or ".." in re.split(r"[/\\]+", rel) or not rel.lower().endswith(".bin")):
        raise _bad_firmware(f"bad image path {rel!r} (a relative .bin path)")
    path = os.path.join(build_dir, rel)
    if not security.within(path, build_dir):
        raise _bad_firmware(f"image {rel!r} resolves outside the firmware dir")
    if not os.path.isfile(path):
        raise ActionError(f"missing image {path}", code="bad_firmware")
    return addr, path


def build_flash_argv(port: str, build_dir: str, args: dict, baud: int, erase: bool,
                     kind: str | None = None) -> list:
    if not isinstance(args, dict):
        raise _bad_firmware("not a JSON object")
    extra = args.get("extra_esptool_args") or {}
    files = args.get("flash_files") or {}
    if not isinstance(extra, dict) or not isinstance(files, dict):
        raise _bad_firmware("extra_esptool_args / flash_files must be objects")
    if not files:
        raise ActionError("flasher_args.json lists no flash_files", code="bad_firmware")
    try:
        security.check_argv_value(port)
    except ValueError as e:
        raise ActionError(str(e), code="port_error") from None
    argv = esptool_command()
    if extra.get("stub") is False:
        argv.append("--no-stub")
    argv += ["--port", port]
    if kind != PORT_USB_SERIAL_JTAG:
        # A native USB-Serial-JTAG port is CDC-ACM: there is no transfer baud
        # rate there, and esptool takes --port (not --serial) in v5.x.
        argv += ["--baud", str(int(baud))]
    chip = extra.get("chip")
    if chip:
        if not isinstance(chip, str) or not CHIP_RE.match(chip):
            raise _bad_firmware(f"bad chip {chip!r}")
        argv += ["--chip", chip]
    before = extra.get("before", "default_reset")
    after = extra.get("after", "hard_reset")
    for what, mode in (("before", before), ("after", after)):
        if mode not in ESPTOOL_RESET_MODES:
            raise _bad_firmware(f"bad {what} reset mode {mode!r}")
    argv += ["--before", before, "--after", after, "write_flash"]
    if erase:
        argv.append("--erase-all")
    argv += _checked_write_flash_args(args.get("write_flash_args") or [])
    images = [_checked_image(build_dir, addr, rel) for addr, rel in files.items()]
    for addr, path in sorted(images, key=lambda ap: int(ap[0], 0)):
        argv += [addr, path]
    return argv


def esptool_reset_argv(port: str) -> list:
    """A reset-only call for a board with no RTS pin (native USJ).

    esptool has no `probe`/`info` command in v5.x and no reset command; an
    identify command (`flash-id` reads the flash geometry, writes nothing) run
    with the default `--after hard_reset` ends with esptool's own hard reset,
    which is what resets the chip on that port."""
    security.check_argv_value(port)
    return esptool_command() + ["--port", port, "--after", "hard_reset", "flash-id"]


def run_tool(ctx: JobContext, argv: list) -> tuple[int, str]:
    ctx.log("$ " + " ".join(shlex.quote(a) for a in argv))
    try:
        # argv list, no shell: values from requests/firmware dirs were validated
        # (build_flash_argv, security.check_argv_value).
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,  # nosec B603
                                stderr=subprocess.STDOUT, text=True, errors="replace")
    except FileNotFoundError as e:
        raise ActionError(f"can't run {argv[0]}: {e}", code="tool_missing") from None
    out = []

    def pump():
        for line in proc.stdout:
            line = line.rstrip()
            out.append(line)
            # esptool's progress lines are noisy; keep the transcript readable.
            if not re.match(r"^Writing at 0x[0-9a-f]+", line):
                ctx.log(line)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        while proc.poll() is None:
            try:
                ctx.check()
            except BaseException:
                proc.kill()
                raise
            time.sleep(0.1)
    finally:
        t.join(2)
        proc.stdout.close()
    return proc.returncode, "\n".join(out)


# ───────────────────────────── the plugin ─────────────────────────────


class Esp32Plugin(Plugin):
    """The `esp32-server` plugin: owns the `role: hub` boards ("server" boards)
    and the fake software server. Sensor boards belong to the sibling
    esp32-sensor plugin."""
    name = PLUGIN_NAME
    version = VERSION
    capabilities = ["events", "uploads"]

    def __init__(self, ports=None, baud=None, flash_baud=None,
                 open_serial=None, firmware_root=None, rescan=True,
                 fake_hub_mode=None, fake_hub_port=None, fake_hub_bind=None,
                 fake_hub_address=None, mdns_advertiser=None, log=None,
                 lock_dir=None, role_cache=None):
        super().__init__()
        if os.environ.get("PLUGIN_UPLOAD_DIR"):
            self.uploads = Uploads(os.environ["PLUGIN_UPLOAD_DIR"])
        self.port_specs = ports if ports is not None else self._default_port_specs()
        self.baud = baud or _env_int("ESP32_BAUD", 115200)
        self.flash_baud = flash_baud or _env_int("ESP32_FLASH_BAUD", 460800)
        self.firmware_root = firmware_root or os.environ.get("ESP32_FIRMWARE_ROOT") or os.getcwd()
        self._open_serial = open_serial
        self._boards: dict[str, Board] = {}  # port -> Board
        self._lock = threading.Lock()
        self._error = None
        self.jobs = None  # set by main() / tests, to report `busy`
        # Cross-process board ownership (shared with esp32-sensor): that plugin
        # must never open this plugin's boards' consoles (two reader threads on
        # one tty shred each other's bytes). A per-node flock says which plugin
        # has a console; the role cache lets this plugin skip a node whose role
        # already belongs to the other one. A plugin that finds a board of the
        # other role drops it and hands its lock over on the next rescan.
        self.lock_dir = lock_dir if lock_dir is not None else (
            os.environ.get("ESP32_LOCK_DIR") or LOCK_DIR_DEFAULT)
        self.role_cache = role_cache if role_cache is not None else (
            os.environ.get("ESP32_ROLE_CACHE") or ROLE_CACHE_DEFAULT)
        self._port_locks: dict[str, int] = {}  # port -> open lock fd
        self._cache_lock = threading.Lock()
        mode = (fake_hub_mode or os.environ.get("ESP32_FAKE_HUB") or "auto").strip().lower()
        if mode not in FAKE_HUB_MODES:
            raise ValueError(f"ESP32_FAKE_HUB must be auto, on or off (got {mode!r})")
        self.fake_hub_mode = mode
        self._fake_lock = threading.Lock()
        self.fake_hub: FakeHub | None = None
        if mode != "off":
            self.fake_hub = FakeHub(
                port=fake_hub_port if fake_hub_port is not None
                else _env_int("ESP32_FAKE_HUB_PORT", 8092),
                # LAN on purpose: the real sensor board reaches the fake hub
                # over Wi-Fi (SECURITY.md, "The fake hub").
                bind=fake_hub_bind or os.environ.get("ESP32_FAKE_HUB_BIND")
                or FAKE_HUB_DEFAULT_BIND,
                address=fake_hub_address or os.environ.get("ESP32_FAKE_HUB_ADDR") or None,
                fw=VERSION, advertiser_factory=mdns_advertiser,
                log=log or (lambda m: print(m, flush=True)))
        self._actions = self._build_actions()
        self._scanned = False  # no fake-hub decision before the first scan found every board
        self.rescan()
        self._scanned = True
        self.update_fake_hub()
        if rescan:
            threading.Thread(target=self._rescan_loop, daemon=True).start()

    # ── discovery ──────────────────────────────────────────────

    @staticmethod
    def _default_port_specs() -> list:
        env = os.environ.get("ESP32_PORTS")
        if env:
            return [p.strip() for p in env.split(",") if p.strip()]
        return DEFAULT_GLOBS.get(platform.system().lower(), DEFAULT_GLOBS["linux"])

    def _candidate_ports(self) -> list:
        seen_real, ports = set(), []
        for spec in self.port_specs:
            matches = sorted(glob.glob(spec)) if any(c in spec for c in "*?[") else [spec]
            for p in matches:
                if not os.path.exists(p):
                    continue
                real = os.path.realpath(p)
                if real in seen_real:  # /dev/serial/by-id/x and /dev/ttyUSB0 are one board
                    continue
                seen_real.add(real)
                ports.append(p)
        return ports

    def rescan(self) -> None:
        ports = self._candidate_ports()
        with self._lock:
            known = dict(self._boards)
        for port, b in known.items():
            if not b.online and not b._paused.is_set():
                b.stop()
                self._drop_lock(port)
                with self._lock:
                    self._boards.pop(port, None)
                self.events.publish(b.id, "state", {"state": "removed", "port": port})
        # Keep cached roles current for the boards we own: a board reflashed
        # with the other role's firmware must be handed over, not hoarded.
        for b in list(self.boards()):
            if b.online and not b._paused.is_set():
                self._cache_role(b)
                if b.vt and not self._owns(b.role):
                    # It answers status and says the other role: the firmware
                    # changed under us. Drop it; the other plugin adopts it.
                    b.stop()
                    self._drop_lock(b.port)
                    with self._lock:
                        self._boards.pop(b.port, None)
        for port in ports:
            with self._lock:
                known_board = self._boards.get(port)
            if known_board is not None:
                # Not identified yet (booting, or flashed with non-VT firmware since)?
                # Ask again, unless a job owns the board.
                if (known_board.mac is None and known_board.online
                        and not known_board._paused.is_set()
                        and not (self.jobs and self.jobs.is_busy(known_board.id))):
                    threading.Thread(target=self._identify, args=(known_board,),
                                     daemon=True).start()
                continue
            cached = self._cached_role(port)
            if cached and not self._owns(cached):
                continue  # known sensor node: skip without touching its console
            fd = try_lock_port(self.lock_dir, port) if self.lock_dir else False
            if fd is None:  # the other plugin has this console; retry next scan
                continue
            if fd is not False:
                with self._lock:
                    self._port_locks[port] = fd
            moved = self._moved_board(port, ports)
            if moved is not None:
                # The kernel re-created the interface under a new index: same board,
                # new node. Rebind it instead of registering the board twice.
                self._rebind(moved, port, fd)
                self.events.publish(moved.id, "state",
                                    {"state": "ready", "port": port,
                                     "port_kind": moved.kind})
                continue
            b = Board(port, self.baud, self.events, open_serial=self._open_serial,
                      mac=usb_mac(port), reset_tool=self._tool_hard_reset)
            try:
                b.start()
            except Exception as e:  # noqa: BLE001 - busy/permission: report, retry later
                self._error = f"{port}: {e}"
                self._drop_lock(port)
                continue
            with self._lock:
                self._boards[port] = b
            threading.Thread(target=self._identify, args=(b,), daemon=True).start()
        self._error = None if ports or not self.port_specs else self._error

    def _identify(self, b: Board) -> None:
        if not b.begin_identify():
            return  # an earlier identify of this board is still waiting
        try:
            self._identify_once(b)
        finally:
            b.end_identify()

    def _identify_once(self, b: Board) -> None:
        owned = False
        # Opening a CP2102N port resets the board, so the first `status` often
        # goes out while it is still booting and gets no reply. The firmware's
        # `@vt - ready` line (which carries the role) follows within a second:
        # take that as the answer instead of dropping the board, which would
        # close the port, reset the board again on the next rescan, and loop.
        if b.probe() or b.wait_ready_status(IDENTIFY_READY_WAIT_S):
            self._cache_role(b)
            owned = self._owns(b.role)
        if not owned:
            # Sensor board — or a board that never answers VT control, which
            # counts as a sensor: give the console back; esp32-sensor adopts
            # it on its next rescan.
            b.stop()
            self._drop_lock(b.port)
            with self._lock:
                self._boards.pop(b.port, None)
            return
        if b.mac:
            b.id = b.mac
        self.events.publish(b.id, "state", {"state": "ready", "port": b.port,
                                            "port_kind": b.kind,
                                            "vt_control": True, "role": b.role})
        self.update_fake_hub()

    # ── the device node can move (native USB-Serial-JTAG) ─────────

    def _rebind(self, b: Board, port: str, fd=None) -> None:
        """Point the Board and the port map at a new node. The MAC is the board's
        identity, so this is the same board: jobs, events and `device=` refs
        follow it; `details.port` says which node it is on now."""
        with self._lock:
            self._boards.pop(b.port, None)
            self._boards[port] = b
        self._drop_lock(b.port)  # release the old node's lock
        if fd is not False:
            self._port_locks[port] = fd
        b.rebind(port)

    def _moved_board(self, port: str, ports: list) -> Board | None:
        """A Board whose MAC matches this node's USB metadata and whose own node
        is gone: the kernel re-created the interface under a new index. Skipped
        while a job owns the board (that job has the port)."""
        mac = usb_mac(port)
        if not mac:
            return None
        for b in self.boards():
            if b.mac != mac or b.kind != port_kind(port):
                continue
            if self.jobs and self.jobs.is_busy(b.id):
                continue
            if any(os.path.realpath(p) == os.path.realpath(b.port) for p in ports):
                continue
            return b
        return None

    # ── cross-process board ownership (shared with esp32-sensor) ─

    def _owns(self, role: str) -> bool:
        return role in OWNED_BOARD_ROLES

    def _drop_lock(self, port: str) -> None:
        """Close this port's lock fd, releasing the flock for the other plugin."""
        fd = self._port_locks.pop(port, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _cache_role(self, b: Board) -> None:
        if not self.role_cache:
            return
        keys = [os.path.realpath(b.port)]
        if b.mac:
            keys.append(b.mac)
        with self._cache_lock:
            for key in keys:
                lock_cache_write(self.role_cache, key, b.role)

    def _cached_role(self, port: str):
        """The role the cache attributes to this node (by MAC when the node
        carries one, else by its real device path), or None when unknown."""
        if not self.role_cache:
            return None
        key = usb_mac(port) or os.path.realpath(port)
        return lock_cache_read(self.role_cache).get(key)

    def _node_after_reset(self, b: Board) -> str | None:
        """The node that took over `b`'s interface after esptool's hard reset
        moved it (an index change on a native port), or None when the node is
        still there or which node it is can't be told."""
        ports = self._candidate_ports()
        real = os.path.realpath(b.port)
        if any(os.path.realpath(p) == real for p in ports):
            return None
        bound = {os.path.realpath(bb.port) for bb in self.boards() if bb is not b}
        cands = [p for p in ports
                 if os.path.realpath(p) not in bound and port_kind(p) == b.kind]
        if b.mac:
            exact = [p for p in cands if usb_mac(p) == b.mac]
            if exact:
                return exact[0]
            if any(usb_mac(p) for p in cands):  # metadata says: other boards
                return None
        return cands[0] if len(cands) == 1 else None

    def _reopen_after_tool(self, ctx: JobContext | None, b: Board) -> None:
        """Reopen the port after esptool, which ends with its own hard reset.

        On a native USB-Serial-JTAG port that reset makes the chip drop off the
        bus: the node (and its by-id link) vanishes for a moment and comes back,
        possibly under a new index, and udev may not have set its permissions
        yet. Wait for it (USJ_REAPPEAR_S) and retry the open until then instead
        of failing the job on the first try."""
        if b.kind == PORT_UART_BRIDGE:
            b.resume()
            return
        deadline = time.monotonic() + USJ_REAPPEAR_S
        time.sleep(0.3)  # let the old node go away before looking for it
        last = None
        while True:
            new = self._node_after_reset(b)
            if new:
                self._rebind(b, new)
                if ctx:
                    ctx.log(f"the kernel re-created the interface as {new} "
                            f"(still the board {b.id})")
            real = os.path.realpath(b.port)
            if any(os.path.realpath(q) == real for q in self._candidate_ports()):
                try:
                    b.resume()
                    return
                except Exception as e:  # noqa: BLE001 - node not ready yet
                    last = e
            if time.monotonic() >= deadline:
                if last is not None:
                    raise last
                raise ActionError(f"{b.port} did not come back within "
                                  f"{USJ_REAPPEAR_S:g} s after the reset",
                                  code="port_error")
            time.sleep(0.2)

    def _tool_hard_reset(self, b: Board, ctx: JobContext | None) -> None:
        """Reset a board whose port has no RTS pin (native USJ): esptool on that
        same port, which ends with esptool's own hard reset. esptool has no
        `probe`/`info` command in v5.x; an identify command writes nothing, and
        ESP32-C3 has no chip id (esptool falls back to the MAC) — not a failure.
        After it the node may have moved, so the Board rebinds before resuming."""
        argv = esptool_reset_argv(b.port)
        b.pause()
        try:
            if ctx is None:  # outside a job: no transcript to pump into
                proc = subprocess.run(argv, stdout=subprocess.PIPE,  # nosec B603 - argv list
                                      stderr=subprocess.STDOUT, text=True,
                                      errors="replace", check=False)
                rc, out = proc.returncode, proc.stdout
            else:
                rc, out = run_tool(ctx, argv)
        finally:
            self._reopen_after_tool(ctx, b)
        if rc != 0:
            tail = "\n".join(out.splitlines()[-5:])
            raise ActionError(f"esptool reset exited {rc} on {b.port}: {tail}",
                              code="reset_failed")

    # ── the fake hub ──────────────────────────────────────────

    def _identifying(self) -> list:
        return [b for b in self.boards() if b.online and b.vt is None]

    def update_fake_hub(self) -> None:
        """Start/stop the fake hub. `on`: always running. `auto`: running while no
        `role: hub` board is attached (decided once every board has answered or
        failed its status probe), so sensors and the app move to a real hub as soon
        as one appears. `off`: never."""
        hub = self.fake_hub
        if hub is None or not self._scanned:
            return
        with self._fake_lock:
            if self.fake_hub_mode == "on":
                want = True
            else:
                if self._identifying():
                    return
                want = not any(b.role == "hub" for b in self.boards() if b.online)
            if want and not hub.active:
                try:
                    hub.start()
                except OSError:
                    self.events.publish(hub.id, "state", {"state": "error", "error": hub.error})
                    return
                self.events.publish(hub.id, "state", {"state": "ready", "role": "hub",
                                                      "fake": True, "url": hub.url})
            elif not want and hub.active:
                hub.stop()
                self.events.publish(hub.id, "state", {"state": "removed", "role": "hub",
                                                      "fake": True,
                                                      "reason": "a hub board is attached"})

    def _fake_hub_json(self) -> dict:
        hub = self.fake_hub
        busy = bool(self.jobs and self.jobs.is_busy(hub.id))
        st = hub.status()
        return {"id": hub.id, "kind": "esp32", "model": "fake hub",
                "state": "busy" if busy else "ready",
                "details": {"role": "hub", "fake": True, "mac": hub.mac, "url": hub.url,
                            "device": st["device"], "ip": st["ip"], "port": st["port"],
                            "sensors": st["sensors"], "rules": st["rules"],
                            "mdns": st["mdns"]}}

    def close(self) -> None:
        if self.fake_hub is not None:
            self.fake_hub.stop()  # unregister the fake server's mDNS name
        for port in list(self._port_locks):
            self._drop_lock(port)  # give the boards back to esp32-sensor

    def _rescan_loop(self) -> None:
        while True:
            time.sleep(RESCAN_S)
            try:
                self.rescan()
                self.update_fake_hub()
            except Exception as e:  # noqa: BLE001
                self._error = f"rescan failed: {e}"

    def boards(self) -> list:
        with self._lock:
            return list(self._boards.values())

    # ── Plugin interface ──────────────────────────────────────

    def devices(self) -> list:
        out = []
        for b in self.boards():
            busy = bool(self.jobs and self.jobs.is_busy(b.id))
            out.append(b.to_json(busy))
        if self.fake_hub is not None and self.fake_hub.active:
            out.append(self._fake_hub_json())
        return out

    def device_error(self):
        if self._error:
            return self._error
        if self.fake_hub is not None and self.fake_hub.error:
            return f"fake hub: {self.fake_hub.error}"
        return None

    def _wait_identified(self) -> None:
        end = time.monotonic() + IDENTIFY_WAIT_S
        while self._identifying() and time.monotonic() < end:
            time.sleep(0.05)

    def resolve_device(self, ref):
        """`device=` is a MAC (or serial port), `hub`/`server` or omitted: this
        plugin only owns hub/server boards and the fake server; sensor boards
        belong to the esp32-sensor plugin."""
        try:
            ref = security.check_device_ref(
                ref, [b.port for b in self.boards()] + [FAKE_HUB_ID])
        except ValueError as e:
            # Same answer as any other unknown board (PLUGIN_API.md), with the reason.
            raise HttpError(404, "unknown_device", f"no board like that: {e}") from None
        if ref in (None, "", "hub", "server"):
            return self._resolve_hub()
        if ref == "sensor":
            raise HttpError(404, "wrong_plugin",
                            "sensor boards belong to the esp32-sensor plugin; "
                            "this plugin only owns hub/server boards and the fake server")
        hub = self.fake_hub
        if hub is not None and isinstance(ref, str) and ref.lower() in (hub.id, hub.mac):
            if not hub.active:
                raise HttpError(404, "unknown_device",
                                "the fake hub isn't running (a hub board is attached, or "
                                f"it failed to start: {hub.error})")
            return hub.id, hub
        for b in self.boards():
            if ref in (b.id, b.mac, b.port) or os.path.realpath(str(ref)) == os.path.realpath(b.port):
                return b.id, b
        raise HttpError(404, "unknown_device", f"no board {ref!r}")

    def _resolve_hub(self):
        hub = self.fake_hub
        if self.fake_hub_mode == "on":
            self.update_fake_hub()
            if not hub.active:
                raise HttpError(404, "unknown_device", f"the fake hub isn't running: {hub.error}")
            return hub.id, hub
        self._wait_identified()
        hubs = [b for b in self.boards() if b.role == "hub"]
        if len(hubs) == 1:
            return hubs[0].id, hubs[0]
        if len(hubs) > 1:
            raise HttpError(400, "bad_request",
                            f"{len(hubs)} hub boards attached; pass device=<mac> "
                            f"({', '.join(b.id for b in hubs)})")
        if hub is None:
            raise HttpError(404, "unknown_device",
                            "no hub board attached (and ESP32_FAKE_HUB=off)")
        self.update_fake_hub()
        if not hub.active:
            raise HttpError(404, "unknown_device", f"no hub board attached and the fake hub "
                            f"isn't running: {hub.error}")
        return hub.id, hub

    def firmware_roots(self) -> list:
        """Where a firmware_dir may point: ESP32_FIRMWARE_ROOT, the extra
        ESP32_FIRMWARE_ROOTS, the upload dir, and /nix/store (a Nix `result`
        link resolves there; the store is world-readable anyway)."""
        roots = [self.firmware_root, self.uploads.root]
        roots += [r for r in os.environ.get("ESP32_FIRMWARE_ROOTS", "").split(os.pathsep) if r]
        if os.path.isdir("/nix/store"):
            roots.append("/nix/store")
        return roots

    def resolve_path(self, value: str) -> str:
        if value.startswith("upload:"):
            return self.uploads.resolve(value)
        try:
            return security.contained_path(value, self.firmware_root, self.firmware_roots())
        except ValueError as e:
            raise HttpError(400, "bad_request", f"firmware_dir: {e}") from None

    def actions(self) -> dict:
        return self._actions

    # ── actions ───────────────────────────────────────────────

    def _build_actions(self) -> dict:
        acts = [
            Action("flash",
                   "Write a prebuilt ESP-IDF firmware directory (containing flasher_args.json, "
                   "e.g. a Nix `result/` or `result/build`) to the board, then wait for the "
                   "firmware's `@vt - ready` line.",
                   self.act_flash,
                   {"firmware_dir": Param("path", "build directory on the plugin host, "
                                          "or upload:<id>", required=True),
                    "erase": Param("bool", "erase the whole flash first (also clears NVS)",
                                   default=False),
                    "wait_ready": Param("bool", "require the VT ready line after boot",
                                        default=True),
                    "baud": Param("int", "esptool baud rate", default=None)},
                   default_timeout_ms=180_000),
            Action("reset", "Hard-reset the board through EN and wait until it is ready. "
                   "Clears every expectation.",
                   self.act_reset,
                   {"wait_ready": Param("bool", "require the VT ready line", default=True)},
                   default_timeout_ms=30_000),
            Action("factory-reset",
                   "Put the board back to factory settings: forget the provisioned Wi-Fi "
                   "network, drop every rule, reboot, and wait until it is ready. Use it as a "
                   "test's first device step.",
                   self.act_factory_reset, default_timeout_ms=30_000),
            Action("expect",
                   "Apply changes to how the device answers the app: each line of `script` is "
                   "sent as a rule, on top of the rules already loaded (state only what "
                   "changes; `<METHOD> <path> -> normal` undoes one rule, `clear` undoes all). "
                   "The rule language is the firmware's (Esp32-Sensor "
                   "docs/vt-control.md, hub-protocol.md section 6): HTTP rules go to "
                   "device=hub (the fake hub applies them itself), `report`/`temp`/`wifi` "
                   "directives to the sensor. Blank and # lines are skipped. Stops at the "
                   "first line the device rejects.",
                   self.act_expect,
                   {"script": Param("string", "rule lines, one per line (the test's inline "
                                    "block)", required=True)},
                   default_timeout_ms=30_000),
            Action("clear", "Drop every expectation: the board behaves normally again.",
                   self.act_clear, default_timeout_ms=10_000),
            Action("forget",
                   "Hub only (device=hub): forget a sensor, like DELETE /devices/<mac> "
                   "(registry entry and history). A sensor that still reports registers "
                   "again on its next report.",
                   self.act_forget,
                   {"mac": Param("string", "the sensor's MAC (aa:bb:cc:dd:ee:ff)",
                                 required=True)},
                   default_timeout_ms=10_000),
        ]
        return {a.name: a for a in acts}

    # helpers

    @staticmethod
    def _fake(ctx: JobContext) -> FakeHub | None:
        return ctx.device if isinstance(ctx.device, FakeHub) else None

    def _not_on_fake_hub(self, ctx: JobContext, action: str) -> None:
        if self._fake(ctx):
            raise ActionError(f"{action} is not supported on the fake hub (it is software on "
                              "the plugin host: no firmware, reset line or radio)",
                              code="unsupported")

    @staticmethod
    def _rule_rejected(n: int, rule: str, msg) -> ActionFailed:
        return ActionFailed(
            f"rule {n} rejected by the board: {msg}: {rule} "
            f"(lines 1-{n - 1} of this block were applied)" if n > 1 else
            f"rule {n} rejected by the board: {msg}: {rule}",
            code="rule_rejected",
            result={"line": n, "rule": rule, "error": msg, "applied": n - 1})

    def _wait_ready_line(self, ctx: JobContext, b: Board, since_seq: int,
                         probe_after: float | None = None) -> dict:
        """Wait for `@vt - ready` after `since_seq`. With `probe_after`, a board that
        answers `status` after that many seconds also counts: the ready line can
        be printed before the port is reopened after esptool's reset."""
        start = time.monotonic()
        end = start + min(READY_TIMEOUT_S, max(ctx.remaining(), 0))
        while b.last_ready_seq <= since_seq:
            ctx.check()
            if probe_after is not None and time.monotonic() - start > probe_after:
                probe_after = None  # once
                reply = None
                try:
                    reply = b.request(ctx, "status", timeout=1.5)
                except ActionFailed:
                    pass
                if reply is not None and reply.ok:
                    ctx.log("ready line not seen (printed before the port reopened); "
                            "board answers status")
                    if b.mac and b.id != b.mac:
                        b.id = b.mac
                    return dict(reply.payload)
            if time.monotonic() > end:
                raise ActionFailed("the board didn't print `@vt - ready` after boot "
                                   "(crashed, or firmware built without VT_CONTROL)",
                                   code="not_ready")
            time.sleep(0.05)
        ctx.log("board is ready")
        if b.mac and b.id != b.mac:
            b.id = b.mac
        return dict(b.status or {})

    def act_flash(self, ctx: JobContext, p: dict):
        self._not_on_fake_hub(ctx, "flash")
        b: Board = ctx.device
        build_dir, args = find_flasher_args(p["firmware_dir"])
        argv = build_flash_argv(b.port, build_dir, args,
                                p["baud"] or self.flash_baud, p["erase"], b.kind)
        ctx.log(f"flashing {build_dir} -> {b.port}")
        b.pause()
        try:
            rc, out = run_tool(ctx, argv)
        finally:
            seq = b.line_seq
            try:
                self._reopen_after_tool(ctx, b)
            except Exception as e:  # noqa: BLE001
                raise ActionError(f"couldn't reopen {b.port} after flashing: {e}",
                                  code="port_error") from None
        if rc != 0:
            tail = "\n".join(out.splitlines()[-5:])
            raise ActionError(f"esptool exited {rc}: {tail}", code="flash_failed")
        chip = (args.get("extra_esptool_args") or {}).get("chip")
        if chip:
            b.model = chip.upper().replace("ESP32C", "ESP32-C").replace("ESP32S", "ESP32-S")
        result = {"firmware_dir": build_dir, "chip": chip}
        if p["wait_ready"]:
            # esptool resets the board at the end; the port reopened right after.
            result["status"] = self._wait_ready_line(ctx, b, seq, probe_after=3.0)
        return result

    def act_reset(self, ctx: JobContext, p: dict):
        self._not_on_fake_hub(ctx, "reset")
        b: Board = ctx.device
        seq = b.line_seq
        if b.kind == PORT_UART_BRIDGE:
            ctx.log("pulsing EN (RTS)")
        else:
            ctx.log(f"no RTS pin on a native port: resetting with esptool on {b.port}")
        b.hard_reset(ctx)
        if p["wait_ready"]:
            return {"status": self._wait_ready_line(ctx, b, seq)}
        return {}

    def act_factory_reset(self, ctx: JobContext, p: dict):
        hub = self._fake(ctx)
        if hub:
            hub.factory_reset()
            ctx.log("fake hub: registry, history and rules erased")
            return {"status": hub.status()}
        b: Board = ctx.device
        seq = b.line_seq
        reply = b.command(ctx, "factory-reset")
        if not (isinstance(reply, dict) and reply.get("rebooting")):
            raise ActionFailed(f"unexpected reply to factory-reset: {reply}",
                               result={"reply": reply})
        status = self._wait_ready_line(ctx, b, seq)
        if status.get("rules"):
            raise ActionFailed(f"board still has {status['rules']} rule(s) after reboot",
                               result={"status": status})
        if status.get("role") == "hub":
            ctx.log(f"factory settings: {status.get('sensors')} sensor(s) known")
        else:
            ctx.log(f"factory settings: station ssid {status.get('ssid')!r}, "
                    f"provisioning ssid {status.get('ap_ssid')!r}")
        return {"status": status}

    def act_expect(self, ctx: JobContext, p: dict):
        """Apply a block of changes. Nothing is cleared first: rules accumulate on
        the board until a later block overrides them, `-> normal` removes one, or
        `clear` drops them all."""
        rules = [ln.strip() for ln in p["script"].splitlines()]
        rules = [ln for ln in rules if ln and not ln.startswith("#")]
        if not rules:
            raise ActionError("script has no rules", code="bad_request")
        hub = self._fake(ctx)
        count = None
        for n, rule in enumerate(rules, 1):
            if hub:
                ctx.log(f"fake hub <- rule {rule}")
                try:
                    count = hub.add_rule(rule)
                except ValueError as e:
                    raise self._rule_rejected(n, rule, str(e)) from None
                continue
            reply = ctx.device.request(ctx, "rule " + rule)
            if not reply.ok:
                msg = (reply.payload.get("error") if isinstance(reply.payload, dict)
                       else reply.payload)
                raise self._rule_rejected(n, rule, msg)
            if isinstance(reply.payload, dict):
                count = reply.payload.get("rules", count)
        return {"rules": count, "applied": len(rules)}

    def act_clear(self, ctx: JobContext, p: dict):
        hub = self._fake(ctx)
        if hub:
            hub.clear()
            return {"rules": 0}
        return ctx.device.command(ctx, "clear")

    def act_forget(self, ctx: JobContext, p: dict):
        mac = p["mac"].strip().lower()
        if not MAC_RE.match(mac):
            raise ActionError(f"bad mac {p['mac']!r} (expected aa:bb:cc:dd:ee:ff)",
                              code="bad_request")
        hub = self._fake(ctx)
        if hub:
            if not hub.forget(mac):
                raise ActionFailed(f"the hub doesn't know {mac}", code="device_error",
                                   result={"mac": mac})
            return {"forgotten": mac, "sensors": hub.status()["sensors"]}
        b: Board = ctx.device
        if b.role != "hub":
            raise ActionError("forget is a hub action; use device=hub", code="wrong_role")
        reply = b.command(ctx, f"forget {mac}")
        return {"forgotten": mac, "reply": reply}


def main() -> None:
    # Bind/token policy first, before any board is opened: a non-loopback bind
    # without a (long enough) PLUGIN_TOKEN refuses to start (SECURITY.md).
    bind, token = security.bind_and_token_from_env()
    err = security.bind_policy_error(bind, token)
    if err:
        print(f"{PLUGIN_NAME}: {err}", file=sys.stderr, flush=True)
        sys.exit(2)
    plugin = Esp32Plugin()
    server = PluginServer(plugin, bind, _env_int("PLUGIN_PORT", PLUGIN_DEFAULT_PORT), token)
    plugin.jobs = server.jobs
    # systemd/launchd stop with SIGTERM: exit through `finally` so the fake
    # server's mDNS name is dropped and the board locks are released.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"MonkeyTester {plugin.name} {VERSION} (contract {ps.CONTRACT} "
          f"v{ps.CONTRACT_VERSION}) on {bind}:{server.port} "
          f"(token {'required' if token else 'off'}); ports {plugin.port_specs}; "
          f"fake server {plugin.fake_hub_mode}", flush=True)
    for b in plugin.boards():
        print(f"  board on {b.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        plugin.close()  # mDNS goodbye + release the board console locks


if __name__ == "__main__":
    main()
