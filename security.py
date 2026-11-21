"""Bind/token policy and request-input validators for the ESP32 device plugins.

This file is shared VERBATIM by MonkeyTester-Plugin-Esp32 and
MonkeyTester-Plugin-Server: the two plugins run side by side on one host and
must refuse/accept exactly the same things. Change both copies together.

See SECURITY.md for the threat model. Standard library only.
"""

from __future__ import annotations

import ipaddress
import os
import re

# The plugin API listens here unless PLUGIN_BIND says otherwise. Loopback:
# the viewer runs on the same host and reaches the plugins at 127.0.0.1.
DEFAULT_BIND = "127.0.0.1"
# A token guarding a LAN-reachable plugin must not be guessable.
MIN_TOKEN_LEN = 16

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")
# Serial nodes a request may name: udev by-id links, Linux USB-UART / CDC-ACM
# nodes and the macOS call-out devices the plugin scans by default.
SERIAL_PORT_RE = re.compile(
    r"^/dev/(?:serial/by-id/[A-Za-z0-9._:+@=-]+|ttyUSB[0-9]+|ttyACM[0-9]+"
    r"|cu\.[A-Za-z0-9._-]+)$")
JOB_ID_RE = re.compile(r"^j-[0-9a-f]{12}$")
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{12}$")
BOARD_ROLES = ("sensor", "hub", "server")
MAX_REF_LEN = 256
MAX_PATH_LEN = 4096


# ───────────────────────────── bind / token ─────────────────────────────


def is_loopback(host: str | None) -> bool:
    """True for 127.0.0.0/8, ::1 and `localhost`; False for anything else,
    including "" and 0.0.0.0 / :: (every interface)."""
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def bind_policy_error(bind: str | None, token: str | None) -> str | None:
    """Why the plugin must refuse to start on `bind` with `token`, or None.

    Loopback bind: a token is optional. Any other bind (a LAN address,
    0.0.0.0, ::): PLUGIN_TOKEN is mandatory and at least MIN_TOKEN_LEN chars."""
    if is_loopback(bind):
        return None
    if not token:
        return (f"refusing to listen on {bind!r} without PLUGIN_TOKEN: a non-loopback "
                "bind exposes the plugin (flashing, serial consoles) to the network. "
                "Set PLUGIN_TOKEN (e.g. `openssl rand -hex 32`) or bind to 127.0.0.1.")
    if len(token) < MIN_TOKEN_LEN:
        return (f"refusing to listen on {bind!r}: PLUGIN_TOKEN is shorter than "
                f"{MIN_TOKEN_LEN} characters (use e.g. `openssl rand -hex 32`)")
    return None


def bind_and_token_from_env(env=None) -> tuple[str, str | None]:
    """(bind address, token or None) from PLUGIN_BIND / PLUGIN_TOKEN."""
    env = os.environ if env is None else env
    bind = (env.get("PLUGIN_BIND") or "").strip() or DEFAULT_BIND
    token = (env.get("PLUGIN_TOKEN") or "").strip() or None
    return bind, token


# ───────────────────────────── validators ─────────────────────────────


def _plain_str(value, limit: int = MAX_REF_LEN) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    if "\0" in value or len(value) > limit:
        raise ValueError("too long or contains NUL")
    return value


def is_mac(value) -> bool:
    return isinstance(value, str) and bool(MAC_RE.match(value))


def is_serial_port(value) -> bool:
    return (isinstance(value, str) and ".." not in value.split("/")
            and bool(SERIAL_PORT_RE.match(value)))


def is_job_id(value) -> bool:
    return isinstance(value, str) and bool(JOB_ID_RE.match(value))


def is_upload_id(value) -> bool:
    return isinstance(value, str) and bool(UPLOAD_ID_RE.match(value))


def check_device_ref(ref, known_refs=()) -> str | None:
    """A `device=` value: None/"" (default board), a role alias, a MAC, a serial
    port path (by-id / ttyUSB / ttyACM / macOS cu.*), or exactly one of
    `known_refs` (the port strings of boards the plugin already has, the fake
    hub's id). Returns it stripped; raises ValueError."""
    if ref is None:
        return None
    ref = _plain_str(ref).strip()
    if ref == "" or ref in BOARD_ROLES or is_mac(ref) or is_serial_port(ref):
        return ref
    if ref in set(known_refs):
        return ref
    raise ValueError("expected a MAC (aa:bb:cc:dd:ee:ff), a serial port "
                     "(/dev/serial/by-id/…, /dev/ttyUSB*, /dev/ttyACM*) or a role")


def check_argv_value(value: str) -> str:
    """A value placed into a subprocess argv (argv list, never a shell): it must
    not be empty, start with `-` (it would read as an option) or contain NUL."""
    if not isinstance(value, str) or not value or value.startswith("-") or "\0" in value:
        raise ValueError(f"unsafe command-line value {value!r}")
    return value


# ───────────────────────────── paths ─────────────────────────────


def within(path: str, root: str) -> bool:
    """True when realpath(path) is root itself or inside realpath(root)."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([real, base]) == base
    except ValueError:  # different drives / mix of absolute and relative
        return False


def contained_path(value: str, base: str, roots) -> str:
    """Resolve a request path value: relative values join `base`; the result's
    realpath must lie under one of `roots`. Rejects NUL, over-long values and
    `..` components. Returns the realpath; raises ValueError with the reason."""
    value = _plain_str(value, MAX_PATH_LEN)
    if not value:
        raise ValueError("empty path")
    if ".." in re.split(r"[/\\]+", value):
        raise ValueError("`..` is not allowed in paths")
    full = value if os.path.isabs(value) else os.path.join(base, value)
    real = os.path.realpath(full)
    for root in roots:
        if root and within(real, root):
            return real
    raise ValueError("path is outside the allowed roots "
                     "(ESP32_FIRMWARE_ROOT, ESP32_FIRMWARE_ROOTS, uploads)")
