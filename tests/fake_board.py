"""An in-memory stand-in for a serial port with MT-control firmware behind it.

FakeSerial looks enough like pyserial's Serial for board.Board; the firmware side
answers the MT control protocol (the sensor firmware's docs/mt-control.md) and prints
the `@mt - ready` line on boot.

A USJ FakeSerial (kind `usb-serial-jtag`, or a ttyACM-style name) stands for a
native USB-Serial-JTAG port: CDC-ACM, so DTR/RTS raise there and the only reset
left is esptool's own; UsjNodes models the ttyACM index moving after that reset.
"""

import json
import os
import threading
import time

from board import PORT_USB_SERIAL_JTAG, port_kind


SENSOR_NO_HTTP = "no HTTP server; set rules on the hub"


class FakeFirmware:
    """role: None = sensor firmware from before roles (HTTP rules, no `role` in
    its status); "sensor" = the hub-era sensor (no HTTP server: HTTP rules are
    rejected; `report`/`temp` directives); "hub" = the hub board
    (the server firmware's docs/hub-protocol.md section 6)."""

    def __init__(self, mac="7c:df:a1:a4:8f:44", mt=True, boot_delay=0.05,
                 ready_on_boot=True, transport="softap", role=None):
        self.mac = mac
        self.mt = mt
        self.role = role
        self.sensors = set()  # hub: macs it knows
        # "softap" or "ble": the build's provisioning transport. None: firmware
        # from before prov-start (no transport/prov_on/ble_name in its status).
        self.transport = transport
        self.boot_delay = boot_delay
        self.ready_on_boot = ready_on_boot
        self.port = None  # FakeSerial currently attached
        self.received = []
        self.reboots = 0
        self.silent = False  # True: ignore all requests (hung firmware)
        self._reset_state()

    provisioned_ssid = "External"
    ap_on = False
    ap_seconds = None
    prov_on = False

    def _reset_state(self):
        self.rules = {}  # (method, path) -> response text
        self.wifi = "on"
        self.mqtt = "on"
        self.report = "on"
        self.temps = None  # `temp` directive values; None = the probe

    hub_host = "tshub-fake.local:8092"
    last_report = "ok"

    def status(self):
        if self.role == "hub":
            return {"proto": 2, "role": "hub", "device": "tshub-" + self.mac.replace(":", "")[6:],
                    "mac": self.mac, "ip": "192.168.125.20", "ssid": "Home",
                    "rules": len(self.rules), "sensors": len(self.sensors),
                    "time_synced": True, "wifi": self.wifi}
        name = "BJF-" + self.mac.replace(":", "")[6:]
        st = {"proto": 2, "device": "ts-" + self.mac.replace(":", "")[4:], "mac": self.mac,
              "ip": "", "ssid": self.provisioned_ssid or "MyAccessPoint",
              "ap_ssid": name, "ap_on": self.ap_on, "rules": len(self.rules), "wifi": self.wifi, "mqtt": self.mqtt}
        if self.transport is not None:
            st.update(transport=self.transport, prov_on=self.prov_on, ble_name=name)
        if self.role == "sensor":
            st.update(role="sensor", hub=self.hub_host if self.provisioned_ssid else "",
                      report=self.report, last_report=self.last_report)
        return st

    # -- device output --
    def emit(self, line):
        if self.port is not None:
            self.port.feed((line + "\n").encode())

    def boot(self):
        self.reboots += 1
        self._reset_state()

        def run():
            time.sleep(self.boot_delay)
            self.emit("ESP-ROM:esp32c3-api1-20210207")
            self.emit("\x1b[0;32mI (312) temp-sensor: test mode enabled\x1b[0m")
            if self.mt and self.ready_on_boot:
                self.emit("@mt - ready " + json.dumps(self.status()))
        threading.Thread(target=run, daemon=True).start()

    # -- device input --
    def on_line(self, line):
        self.received.append(line)
        if not self.mt or self.silent or not line.startswith("@mt "):
            return
        parts = line[4:].split()
        if len(parts) < 2:
            return
        rid, cmd, args = parts[0], parts[1], parts[2:]
        try:
            payload = self.handle(cmd, args)
        except ValueError as e:
            self.emit(f"@mt {rid} err " + json.dumps({"error": str(e)}))
            return
        self.emit(f"@mt {rid} ok " + json.dumps(payload))
        if payload.get("rebooting"):
            self.boot()

    def add_rule(self, line):
        """A small subset of the firmware's rule parser (docs/mt-control.md)."""
        words = line.split()
        if len(words) == 2 and words[0] in ("wifi", "mqtt") and words[1] in ("on", "off"):
            setattr(self, words[0], words[1])
            return
        if self.role == "sensor":
            if len(words) == 2 and words[0] == "report" and words[1] in ("on", "off"):
                self.report = words[1]
                return
            if words and words[0] == "temp":
                if words[1:] == ["normal"]:
                    self.temps = None
                    return
                vals = words[1:]
                if not 1 <= len(vals) <= 8:
                    raise ValueError("bad temp (1-8 values or normal)")
                for v in vals:
                    try:
                        t = float(v)
                    except ValueError:
                        raise ValueError("bad temp value") from None
                    if not -55 <= t <= 125:
                        raise ValueError("bad temp value")
                self.temps = [float(v) for v in vals]
                return
            if len(words) >= 2 and words[0] in ("GET", "POST", "ANY"):
                raise ValueError(SENSOR_NO_HTTP)
        if len(words) < 4 or words[2] != "->":
            raise ValueError("expected <METHOD> <path> -> <response>")
        method, path = words[0], words[1]
        if method not in ("GET", "POST", "ANY"):
            raise ValueError("bad method")
        resp = line.split("->", 1)[1].strip()
        head = resp.split()[0]
        if head == "sequence":
            values = resp.split()[1:]
            if not 1 <= len(values) <= 8:
                raise ValueError("bad sequence")
            for v in values:
                try:
                    t = float(v)
                except ValueError:
                    raise ValueError("bad sequence value")
                if not -55 <= t <= 125:
                    raise ValueError("bad sequence value")
        elif head not in ("hang", "delay", "normal"):
            if not head.isdigit() or not 100 <= int(head) <= 599:
                raise ValueError("bad status")
        if head == "normal":
            self.rules.pop((method, path), None)
            return
        if (method, path) not in self.rules and len(self.rules) >= 16:
            raise ValueError("too many rules")
        self.rules[(method, path)] = resp

    def handle(self, cmd, args):
        if cmd == "ping":
            return {"proto": 2}
        if cmd == "status":
            return self.status()
        if cmd == "clear":
            self._reset_state()
            return {"rules": 0}
        if cmd == "rule":
            self.add_rule(" ".join(args))
            return {"rules": len(self.rules)}
        if cmd in ("reboot", "factory-reset"):
            if cmd == "factory-reset":
                if self.role == "hub":
                    self.sensors = set()
                else:
                    self.provisioned_ssid = ""
            return {"rebooting": True}
        if self.role == "hub":
            if cmd == "forget" and len(args) == 1:
                if args[0] not in self.sensors:
                    raise ValueError("unknown device")
                self.sensors.discard(args[0])
                return {"sensors": len(self.sensors)}
            raise ValueError("unknown command")
        if cmd == "ap-start" or (cmd == "prov-start" and self.transport is not None):
            if args and (not args[0].isdigit() or int(args[0]) > 86400):
                raise ValueError("bad seconds (0-86400)")
            self.prov_on = True
            self.ap_on = self.transport != "ble"  # BLE builds have no soft-AP
            self.ap_seconds = int(args[0]) if args else None
            return self.status()
        raise ValueError("unknown command")


class FakeSerial:
    """Stands in for pyserial's Serial. `kind="usb-serial-jtag"` (or a ttyACM-style
    port name) is CDC-ACM: no DTR/RTS ioctl, so no pin to pulse and no reset but
    esptool's."""

    def __init__(self, firmware: FakeFirmware, port="/dev/fake0", kind=None):
        self.fw = firmware
        self.port = port
        self.kind = kind or port_kind(port)
        self.usj = self.kind == PORT_USB_SERIAL_JTAG
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._in = b""
        self.is_open = True
        self._dtr = self._rts = False
        firmware.port = self

    def _no_pin(self, name):
        # What pyserial raises for an ioctl the CDC-ACM interface doesn't have.
        raise OSError(f"{self.port} is CDC-ACM: no {name} pin "
                      "(reset it with esptool)")

    # firmware -> host
    def feed(self, data: bytes):
        with self._cond:
            self._buf += data
            self._cond.notify_all()

    def read(self, n):
        with self._cond:
            if not self.is_open:
                raise OSError("port closed")
            if not self._buf:
                self._cond.wait(0.1)
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    # host -> firmware
    def write(self, data: bytes):
        self._in += data
        while b"\n" in self._in:
            line, self._in = self._in.split(b"\n", 1)
            self.fw.on_line(line.decode())
        return len(data)

    def flush(self):
        pass

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        if self.usj:
            raise self._no_pin("DTR")
        self._dtr = v

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        if self.usj:
            raise self._no_pin("RTS")
        if self._rts and not v:  # EN released: the chip boots
            self.fw.boot()
        self._rts = v

    def close(self):
        with self._cond:
            self.is_open = False
            self._cond.notify_all()
        if self.fw.port is self:
            self.fw.port = None


class UsjNodes:
    """A fake `/dev/ttyACM*` enumeration for tests: esptool ends with its hard
    reset, and the kernel re-creates the native interface with the next index
    (`/dev/ttyACM0` -> `/dev/ttyACM1`). The fake esptool logs every run, so the
    enumeration moves with the number of runs."""

    def __init__(self, first="/dev/ttyACM0", log=None, gap=0):
        self.base = first.rstrip("0123456789")
        self.first = int(first[len(self.base):] or 0)
        self.log = log
        # After each esptool run the node is missing for `gap` enumerations, the
        # way a real chip drops off the bus for a moment after its reset.
        self.gap = gap
        self._seen_runs = 0
        self._missing = 0

    def runs(self) -> int:
        if not self.log or not os.path.isfile(self.log):
            return 0
        with open(self.log) as f:
            return sum(1 for line in f if line.strip())

    def ports(self) -> list:
        runs = self.runs()
        if runs != self._seen_runs:
            self._seen_runs = runs
            self._missing = self.gap
        if self._missing > 0:
            self._missing -= 1
            return []
        return [f"{self.base}{self.first + runs}"]
