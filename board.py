"""One ESP32 board on a serial port, speaking the MT control protocol.

The board object owns the port: a reader thread keeps it open, splits the
stream into lines, publishes every line as a `serial` event, and routes
`@mt <id> ok|err ...` replies to whoever sent the request. Flashing pauses the
reader so esptool can have the port.

The protocol itself is specified in the sensor firmware's
docs/mt-control.md.

A board is reached over one of two transports (`port_kind`, see ACTIONS.md): the
CP2102N UART bridge (`uart-bridge`, `/dev/ttyUSB…`) has a baud rate and an EN/RTS
pin; the chip's own USB-Serial-JTAG interface (`usb-serial-jtag`, `/dev/ttyACM…`)
is plain CDC-ACM: the baud rate means nothing there and there is no DTR/RTS to
touch. Everything else (line splitting, MT routing) is the same.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import threading
import time
from collections import deque

import serial  # pyserial

from plugin_server import ActionError, ActionFailed, JobContext

MT_PREFIX = "@mt "
LINE_HISTORY = 2000
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

PORT_UART_BRIDGE = "uart-bridge"
PORT_USB_SERIAL_JTAG = "usb-serial-jtag"


def port_kind(port: str) -> str:
    """Which transport a serial node speaks.

    `uart-bridge`: the CP2102N adapter (`/dev/ttyUSB…`, macOS `cu.usbserial*`) —
    a real baud rate, and a DTR/RTS pair the auto-reset circuit can pulse.
    `usb-serial-jtag`: the chip's own interface (`/dev/ttyACM…`, macOS
    `cu.usbmodem*`) — CDC-ACM: the tty layer ignores the baud rate and pyserial
    raises for the DTR/RTS ioctls the interface doesn't have.

    udev's by-id name (which the node may be reached through, and which fake
    test ports carry) says it in the interface class name: "… USB JTAG/serial
    debug unit" vs "… USB to UART Bridge Controller". Unknown nodes are taken
    for the bridge, which is what this code assumed before."""
    for name in (os.path.basename(port), os.path.basename(os.path.realpath(port))):
        n = name.lower()
        if n.startswith("ttyacm") or "usbmodem" in n:
            return PORT_USB_SERIAL_JTAG
        if n.startswith("ttyusb") or "usbserial" in n:
            return PORT_UART_BRIDGE
        n = name.lower().replace("_", " ")
        if "jtag" in n:
            return PORT_USB_SERIAL_JTAG
        if "bridge" in n or "uart" in n:
            return PORT_UART_BRIDGE
    return PORT_UART_BRIDGE


class VtReply:
    def __init__(self, ok: bool, payload):
        self.ok = ok
        self.payload = payload


class Board:
    def __init__(self, port: str, baud: int, events, open_serial=None,
                 kind: str | None = None, mac: str | None = None,
                 reset_tool=None):
        self.port = port
        self.baud = baud
        self.events = events
        self._open_serial = open_serial or self._default_open
        self._reset_tool = reset_tool
        # Which transport this node speaks: setting the baud rate or DTR/RTS on a
        # native USB-Serial-JTAG port makes pyserial raise.
        self.kind = kind or port_kind(port)
        self._ser = None
        self._lock = threading.RLock()        # guards _ser and writes
        self._cond = threading.Condition()    # lines / replies arrived
        self._lines: deque = deque(maxlen=LINE_HISTORY)  # (seq, monotonic, text)
        self._line_seq = 0
        self._replies: dict[str, VtReply] = {}
        self._ids = itertools.count(1)
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._reader = None
        self.id: str = mac or port          # the MAC when known: from the firmware's
        self.mac: str | None = mac          # status, or from the USB metadata at
        self.model: str | None = None       # discovery (its serial number)
        self.status: dict | None = None     # last status object seen
        self.mt: bool | None = None         # firmware answers MT control?
        self.last_ready_seq = 0
        self.error: str | None = None
        self.online = False
        self._identify_running = False

    # ───────────── port lifecycle ─────────────

    def _default_open(self):
        s = serial.Serial()
        s.port = self.port
        s.timeout = 0.2
        if self.kind == PORT_UART_BRIDGE:
            s.baudrate = self.baud
            # Don't toggle EN/IO0 through the auto-reset circuit just by opening.
            s.dtr = False
            s.rts = False
        # A native USB-Serial-JTAG port is CDC-ACM: it has no baud rate to set and
        # no DTR/RTS ioctls, and pyserial raises for both, so nothing is set.
        s.open()
        return s

    def start(self) -> None:
        self._open()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"serial-{self.port}")
        self._reader.start()

    def _open(self) -> None:
        with self._lock:
            if self._ser is None:
                self._ser = self._open_serial()
                self.online = True
                self.error = None

    def _close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def pause(self) -> None:
        """Release the port (for esptool)."""
        self._paused.set()
        self._close()

    def resume(self) -> None:
        self._open()
        self._paused.clear()

    # ───────────── reading ─────────────

    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            with self._lock:
                ser = self._ser
            if ser is None:
                time.sleep(0.05)
                continue
            try:
                chunk = ser.read(512)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if self._paused.is_set() or self._stop.is_set():
                    continue
                self.online = False
                self.error = f"serial read failed: {e}"
                self.events.publish(self.id, "state", {"state": "offline", "error": self.error})
                self._close()
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._on_line(raw.decode("utf-8", "replace").rstrip("\r"))
            if len(buf) > 4096:  # no newline in a long time: flush it as a line
                self._on_line(buf.decode("utf-8", "replace"))
                buf = b""

    def _on_line(self, text: str) -> None:
        clean = _ANSI.sub("", text)
        with self._cond:
            self._line_seq += 1
            seq = self._line_seq
            self._lines.append((seq, time.monotonic(), clean))
            if clean.startswith(MT_PREFIX):
                self._handle_mt(seq, clean[len(MT_PREFIX):])
            self._cond.notify_all()
        self.events.publish(self.id, "serial", clean)

    def _handle_mt(self, seq: int, rest: str) -> None:
        # Called with self._cond held.
        parts = rest.split(" ", 2)
        if len(parts) < 2:
            return
        rid, kind = parts[0], parts[1]
        payload_raw = parts[2] if len(parts) > 2 else ""
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except json.JSONDecodeError:
            payload = {"raw": payload_raw}
        # Learn the status (and so the role) before `mt` is set: the plugin decides
        # whether to run the fake hub once no board has `mt is None`.
        if rid == "-":
            if kind == "ready":
                self._learn(payload)
                self.last_ready_seq = seq
                self.mt = True
            return
        if kind in ("ok", "err"):
            if kind == "ok" and isinstance(payload, dict) and "proto" in payload:
                self._learn(payload)
            self.mt = True
            self._replies[rid] = VtReply(kind == "ok", payload)

    def _learn(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        if "mac" in payload:
            self.status = payload
            if payload.get("mac") and payload["mac"] != self.mac:
                self.mac = payload["mac"]
            if self.mac and self.id == self.port:
                self.id = self.mac  # first time we learn who this board is

    # ───────────── line access for actions ─────────────

    @property
    def line_seq(self) -> int:
        with self._cond:
            return self._line_seq

    # ───────────── writing / MT requests ─────────────

    def write_line(self, line: str) -> None:
        data = (line + "\n").encode()
        with self._lock:
            if self._ser is None:
                raise ActionError(f"{self.port} is not open", code="port_closed")
            try:
                self._ser.write(data)
                self._ser.flush()
            except (serial.SerialException, OSError) as e:
                raise ActionError(f"serial write failed: {e}", code="port_error") from None

    def request(self, ctx: JobContext | None, command: str, timeout: float = 3.0) -> VtReply:
        """Send `@mt <id> <command>` and wait for its reply."""
        rid = f"p{next(self._ids)}"
        line = f"@mt {rid} {command}"
        if ctx:
            ctx.log(f"-> {line}")
        self.write_line(line)
        end = time.monotonic() + timeout
        with self._cond:
            while rid not in self._replies:
                if ctx:
                    ctx.check()
                left = end - time.monotonic()
                if ctx:
                    left = min(left, ctx.remaining())
                if left <= 0:
                    if ctx:
                        ctx.check()
                    raise ActionFailed(f"no reply to {command!r} within {timeout:.1f}s "
                                       "(firmware without MT_CONTROL?)", code="no_reply")
                self._cond.wait(min(left, 0.25))
            reply = self._replies.pop(rid)
        if ctx:
            ctx.log(f"<- @mt {rid} {'ok' if reply.ok else 'err'} {json.dumps(reply.payload)}")
        return reply

    def command(self, ctx: JobContext | None, command: str, timeout: float = 3.0):
        """Like request(), but an `err` reply is a failed action."""
        reply = self.request(ctx, command, timeout)
        if not reply.ok:
            msg = reply.payload.get("error") if isinstance(reply.payload, dict) else reply.payload
            raise ActionFailed(f"device rejected {command!r}: {msg}", code="device_error",
                               result=reply.payload)
        return reply.payload

    def begin_identify(self) -> bool:
        """Claim the board for one discovery identify; False when one runs."""
        with self._cond:
            if self._identify_running:
                return False
            self._identify_running = True
            return True

    def end_identify(self) -> None:
        with self._cond:
            self._identify_running = False

    def wait_ready_status(self, timeout: float) -> bool:
        """Wait until a `@mt - ready` line (or any status reply) has told us
        the board's status. True when it has, within `timeout` seconds."""
        end = time.monotonic() + timeout
        with self._cond:
            while not (self.mt and self.status):
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.25))
            return True

    def probe(self, timeout: float = 1.5) -> bool:
        """Try a `status` without a job (discovery). Sets mac/status on success."""
        try:
            reply = self.request(None, "status", timeout)
        except (ActionFailed, ActionError):
            self.mt = False if self.mt is None else self.mt
            return False
        return reply.ok

    # ───────────── reset ─────────────

    def hard_reset(self, ctx: JobContext | None = None) -> None:
        """Reset the board on whichever transport it is on.

        `uart-bridge`: pulse EN through the adapter's RTS pin (standard dev-board
        wiring). `usb-serial-jtag`: the native interface has no RTS pin at all, so
        the reset is esptool on this same port — `self._reset_tool`, installed by
        the plugin, which ends with esptool's own hard reset and rebinds the Board
        when the kernel re-created the interface under a new node index."""
        if self.kind != PORT_UART_BRIDGE:
            if self._reset_tool is None:
                raise ActionError(f"{self.port} has no RTS pin and no tool to reset "
                                  f"it with", code="no_reset_pin")
            self._reset_tool(self, ctx)
            return
        with self._lock:
            if self._ser is None:
                raise ActionError(f"{self.port} is not open", code="port_closed")
            self._ser.dtr = False
            self._ser.rts = True
            time.sleep(0.12)
            self._ser.rts = False

    def rebind(self, port: str) -> None:
        """Adopt a new device node (the ttyACM index moves when the kernel
        re-creates the interface after esptool's hard reset). Identity is the MAC,
        so the id, the published events and `device=` references survive."""
        if port == self.port:
            return
        self.port = port
        self.kind = port_kind(port)

    # ───────────── description ─────────────

    @property
    def role(self) -> str:
        """`sensor` or `hub`, from the firmware's status. Firmware from before
        roles (no `role` field) and boards that don't answer MT control count
        as sensors. `server` (the project's rename of "hub") is a status alias
        that normalizes to `hub`, which stays the wire value."""
        role = (self.status or {}).get("role")
        if role == "server":
            return "hub"
        return role if role in ("sensor", "hub") else "sensor"

    def to_json(self, busy: bool) -> dict:
        if not self.online:
            state = "offline"
        elif self.error:
            state = "error"
        elif busy:
            state = "busy"
        else:
            state = "ready"
        details = {"port": self.port, "port_kind": self.kind, "mt_control": self.mt,
                   "role": self.role}
        if self.mac:
            details["mac"] = self.mac
        if self.status:
            for k in ("device", "ip", "ssid", "ap_ssid", "hub", "report", "last_report",
                      "sensors", "time_synced"):
                if k in self.status:
                    details[k] = self.status[k]
        if self.error:
            details["error"] = self.error
        return {"id": self.id, "kind": "esp32", "model": self.model, "state": state,
                "details": details}
