"""The fake hub: a software version of the server firmware, run by the plugin.

When no hub board (`role: hub`) is attached, `device=hub` resolves to this
object. It implements the hub protocol (the server firmware's
docs/hub-protocol.md) sections 1-4 -- mDNS advertisement, `POST /report`, the
registry/history and the app API -- plus the hub's rule language from
section 6, so a test runs unchanged against the fake and a real hub.

Standard library only, except `zeroconf` for the mDNS advertisement, which is
imported when advertising starts.

Security: unlike the plugin API, the fake hub listens on the LAN by default
(FAKE_HUB_DEFAULT_BIND): the real sensor board reaches it over Wi-Fi, exactly as
it would reach a real hub board, and the hub protocol has no authentication
(neither does the firmware). It is a device-protocol endpoint only: it keeps an
in-memory registry and rule table and can reach no file, flash, serial port or
subprocess. Requests are capped (MAX_REQUEST_BYTES -> 413, MAX_PATH_LEN -> 414),
need a valid Content-Length for a body (no chunked bodies), and every
connection has a socket timeout. See SECURITY.md.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

FAKE_HUB_ID = "tshub-fake"          # device id in the plugin, mDNS instance and hostname
FAKE_HUB_MAC = "02:00:00:fa:4e:01"  # locally administered: not a real board's MAC
SERVICE_TYPE = "_tsserver._tcp.local."
PROTO = 1

MAX_SENSORS = 16
HISTORY_POINTS = 180          # one per minute, 3 hours
MAX_REPORT_BYTES = 512
MAX_RULES = 16
MAX_BODY_BYTES = 200
MAX_SEQUENCE = 8
HANG_S = 120.0
MAX_DELAY_MS = 60_000
MAX_REQUEST_BYTES = 4096      # any request body (a report is <= MAX_REPORT_BYTES)
MAX_PATH_LEN = 256
SOCKET_TIMEOUT_S = 10
# Every interface on purpose: the sensor board connects over Wi-Fi.
FAKE_HUB_DEFAULT_BIND = "0.0.0.0"  # nosec B104 - LAN device endpoint by design, see above

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def primary_ipv4() -> str:
    """The address of the interface the default route uses (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ───────────────────────────── rules ─────────────────────────────


class Rule:
    """One parsed `<METHOD> <path> -> <response>` line."""

    def __init__(self, kind: str, status: int | None = None, body: str | None = None,
                 delay_ms: int = 0, values: list | None = None, text: str = ""):
        self.kind = kind            # status | hang | delay | sequence
        self.status = status
        self.body = body
        self.delay_ms = delay_ms
        self.values = values or []
        self.text = text
        self.index = 0
        self.released = threading.Event()  # set when the rule is removed: frees hangs

    def next_value(self) -> float:
        v = self.values[self.index % len(self.values)]
        self.index += 1
        return v


def _parse_status(words: list, line_rest: str) -> tuple[int, str | None]:
    if not words or not words[0].isdigit() or not 100 <= int(words[0]) <= 599:
        raise ValueError("bad status (100-599)")
    parts = line_rest.split(None, 1)
    body = parts[1] if len(parts) > 1 else None
    if body is not None and len(body.encode()) > MAX_BODY_BYTES:
        raise ValueError(f"body too long (max {MAX_BODY_BYTES} bytes)")
    return int(words[0]), body


def parse_rule(line: str):
    """-> ((method, path), Rule) or ((method, path), None) for `-> normal`.
    Raises ValueError with the reason, like the firmware's `err` reply."""
    words = line.split()
    if words and words[0] in ("wifi", "mqtt", "report", "temp"):
        raise ValueError(f"'{words[0]}' is not a hub directive (the fake hub only has HTTP rules)")
    if len(words) < 4 or words[2] != "->":
        raise ValueError("expected <METHOD> <path> -> <response>")
    method, path = words[0], words[1]
    if method not in ("GET", "POST", "ANY"):
        raise ValueError("bad method (GET, POST or ANY)")
    if not path.startswith("/"):
        raise ValueError("bad path (must start with /)")
    resp = line.split("->", 1)[1].strip()
    rwords = resp.split()
    head = rwords[0]
    key = (method, path)
    if head == "normal":
        if len(rwords) != 1:
            raise ValueError("'normal' takes no arguments")
        return key, None
    if head == "hang":
        if len(rwords) != 1:
            raise ValueError("'hang' takes no arguments")
        return key, Rule("hang", text=line)
    if head == "delay":
        if len(rwords) < 3 or not rwords[1].isdigit() or int(rwords[1]) > MAX_DELAY_MS:
            raise ValueError(f"expected delay <ms 0-{MAX_DELAY_MS}> <status> [<body>]")
        rest = resp.split(None, 2)[2]
        status, body = _parse_status(rwords[2:], rest)
        return key, Rule("delay", status, body, delay_ms=int(rwords[1]), text=line)
    if head == "sequence":
        vals = rwords[1:]
        if not 1 <= len(vals) <= MAX_SEQUENCE:
            raise ValueError(f"bad sequence (1-{MAX_SEQUENCE} values)")
        out = []
        for v in vals:
            if not NUMBER_RE.match(v) or not -55 <= float(v) <= 125:
                raise ValueError(f"bad sequence value {v!r} (a number from -55 to 125)")
            out.append(float(v))
        return key, Rule("sequence", values=out, text=line)
    status, body = _parse_status(rwords, resp)
    return key, Rule("status", status, body, text=line)


def _replace_temperatures(obj, value):
    """Every dict with a temperature_c gets `value` (and ok/err of a good reading)."""
    if isinstance(obj, list):
        for x in obj:
            _replace_temperatures(x, value)
    elif isinstance(obj, dict):
        if "temperature_c" in obj:
            obj["temperature_c"] = value
            if "ok" in obj:
                obj["ok"] = True
            if "err" in obj:
                obj["err"] = 0
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _replace_temperatures(v, value)


# ───────────────────────────── mDNS ─────────────────────────────


class MdnsAdvertiser:
    """Advertises `<instance>._tsserver._tcp` on one IPv4 address with python-zeroconf."""

    def __init__(self, instance: str, port: int, txt: dict, address: str):
        self.instance = instance
        self.port = port
        self.txt = txt
        self.address = address
        self._zc = None
        self._info = None

    def start(self) -> None:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf  # optional dependency
        self._info = ServiceInfo(
            SERVICE_TYPE, f"{self.instance}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(self.address)], port=self.port,
            properties=dict(self.txt), server=f"{self.instance}.local.")
        self._zc = Zeroconf(interfaces=[self.address], ip_version=IPVersion.V4Only)
        try:
            self._zc.register_service(self._info)
        except Exception:
            self._zc.close()
            self._zc = None
            raise

    def stop(self) -> None:
        if self._zc is not None:
            try:
                self._zc.unregister_service(self._info)
            finally:
                self._zc.close()
                self._zc = None


# ───────────────────────────── the hub ─────────────────────────────


class FakeHub:
    """Hub protocol sections 1-4 + the hub rule language, in one process."""

    id = FAKE_HUB_ID
    mac = FAKE_HUB_MAC

    def __init__(self, port: int = 8092, bind: str = FAKE_HUB_DEFAULT_BIND,
                 address: str | None = None,
                 fw: str = "0.0.0", advertiser_factory=None, log=None):
        self.port = port
        self.bind = bind
        self.address = address or primary_ipv4()
        self.fw = fw
        self.advertiser_factory = advertiser_factory or MdnsAdvertiser
        self.log = log or (lambda msg: None)
        self.now_wall = time.time        # epoch seconds: *_ms fields, history minutes
        self.now_mono = time.monotonic   # online / age_s
        self._lock = threading.RLock()
        self._sensors: dict = {}         # mac -> registry entry
        self._history: dict = {}         # mac -> OrderedDict(minute -> temperature_c)
        self._rules: dict = {}           # (method, path) -> Rule
        self._httpd = None
        self._advertiser = None
        self._mdns_thread = None
        self.mdns_state = "off"          # off | starting | advertising | error: …
        self.error: str | None = None
        self.active = False

    # ── lifecycle ──────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"http://{self.address}:{self.port}"

    def start(self) -> None:
        with self._lock:
            if self.active:
                return
            try:
                self._httpd = ThreadingHTTPServer((self.bind, self.port), _make_handler(self))
            except OSError as e:
                self.error = f"can't listen on {self.bind}:{self.port}: {e}"
                self.log(f"fake hub: {self.error}")
                raise
            self._httpd.daemon_threads = True
            self.port = self._httpd.server_address[1]
            self.error = None
            self.active = True
            threading.Thread(target=self._httpd.serve_forever, daemon=True,
                             name="fake-hub-http").start()
            self._advertiser = self.advertiser_factory(
                instance=FAKE_HUB_ID, port=self.port,
                txt={"proto": str(PROTO), "mac": self.mac, "fw": self.fw},
                address=self.address)
            self.mdns_state = "starting"
            self._mdns_thread = threading.Thread(target=self._advertise, args=(self._advertiser,),
                                                 daemon=True, name="fake-hub-mdns")
            self._mdns_thread.start()
        self.log(f"fake hub on {self.url} ({self.bind}:{self.port})")

    def _advertise(self, adv) -> None:
        try:
            adv.start()
        except Exception as e:  # noqa: BLE001 - no zeroconf, name taken, no multicast
            with self._lock:
                if self._advertiser is adv:
                    self.mdns_state = f"error: {e!r}"
            self.log(f"fake hub: mDNS advertisement failed: {e!r}")
            return
        with self._lock:
            if self._advertiser is adv:
                self.mdns_state = "advertising"
        self.log(f"fake hub: advertising {FAKE_HUB_ID}.{SERVICE_TYPE} on {self.address}:{self.port}")

    def stop(self) -> None:
        with self._lock:
            if not self.active:
                return
            self.active = False
            httpd, self._httpd = self._httpd, None
            adv, self._advertiser = self._advertiser, None
            thread, self._mdns_thread = self._mdns_thread, None
            for r in self._rules.values():
                r.released.set()  # free held requests
            self.mdns_state = "off"
        if thread is not None:
            thread.join(10)
        if adv is not None:
            try:
                adv.stop()
            except Exception as e:  # noqa: BLE001
                self.log(f"fake hub: mDNS stop failed: {e!r}")
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        self.log("fake hub stopped")

    # ── test control (the plugin's actions) ────────────────────

    def status(self) -> dict:
        with self._lock:
            return {"proto": 2, "role": "hub", "fake": True, "device": FAKE_HUB_ID,
                    "mac": self.mac, "ip": self.address, "port": self.port, "ssid": "",
                    "rules": len(self._rules), "sensors": len(self._sensors),
                    "time_synced": True, "wifi": "on", "mdns": self.mdns_state}

    def add_rule(self, line: str) -> int:
        key, rule = parse_rule(line)
        with self._lock:
            old = self._rules.get(key)
            if rule is None:
                if old is not None:
                    del self._rules[key]
                    old.released.set()
                return len(self._rules)
            if old is None and len(self._rules) >= MAX_RULES:
                raise ValueError(f"too many rules (max {MAX_RULES})")
            self._rules[key] = rule
            if old is not None:
                old.released.set()
            return len(self._rules)

    def clear(self) -> None:
        with self._lock:
            for r in self._rules.values():
                r.released.set()
            self._rules.clear()

    def factory_reset(self) -> None:
        with self._lock:
            self.clear()
            self._sensors.clear()
            self._history.clear()

    def forget(self, mac: str) -> bool:
        mac = mac.lower()
        with self._lock:
            if mac not in self._sensors:
                return False
            del self._sensors[mac]
            self._history.pop(mac, None)
            return True

    def rules(self) -> dict:
        with self._lock:
            return {f"{m} {p}": r.text.split("->", 1)[1].strip()
                    for (m, p), r in self._rules.items()}

    # ── registry ───────────────────────────────────────────────

    def _online(self, e: dict, now_mono: float) -> bool:
        return now_mono - e["last_mono"] < max(3 * e["interval_s"], 15)

    def _entry_json(self, e: dict, now_mono: float) -> dict:
        return {"mac": e["mac"], "device": e["device"], "ip": e["ip"], "fw": e["fw"],
                "online": self._online(e, now_mono),
                "age_s": int(max(0.0, now_mono - e["last_mono"])),
                "last_seen_ms": e["last_ms"], "temperature_c": e["temperature_c"],
                "ok": e["ok"], "err": e["err"]}

    def report(self, raw: bytes) -> tuple[int, dict]:
        if len(raw) > MAX_REPORT_BYTES:
            return 400, {"error": f"body too large (max {MAX_REPORT_BYTES} bytes)"}
        try:
            r = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"error": "body is not JSON"}
        err = _validate_report(r)
        if err:
            return 400, {"error": err}
        mac = r["mac"].lower()
        now_mono, now_wall = self.now_mono(), self.now_wall()
        temp = r["temperature_c"] if r["ok"] else None
        with self._lock:
            if mac not in self._sensors and len(self._sensors) >= MAX_SENSORS:
                offline = [e for e in self._sensors.values() if not self._online(e, now_mono)]
                if not offline:
                    return 507, {"error": "registry full"}
                victim = min(offline, key=lambda e: e["last_mono"])
                self.log(f"fake hub: registry full, evicting {victim['mac']}")
                del self._sensors[victim["mac"]]
                self._history.pop(victim["mac"], None)
            self._sensors[mac] = {
                "mac": mac, "device": r.get("device", ""), "ip": r.get("ip", ""),
                "fw": r.get("fw", ""), "interval_s": r["interval_s"],
                "temperature_c": temp, "ok": r["ok"], "err": r.get("err", 0),
                "last_mono": now_mono, "last_ms": int(now_wall * 1000)}
            if temp is not None:
                hist = self._history.setdefault(mac, OrderedDict())
                minute = int(now_wall // 60)
                hist[minute] = temp
                hist.move_to_end(minute)
                for m in [m for m in hist if m <= minute - HISTORY_POINTS]:
                    del hist[m]
        return 200, {"ok": True}

    def devices_json(self) -> list:
        now = self.now_mono()
        with self._lock:
            return [self._entry_json(self._sensors[m], now) for m in sorted(self._sensors)]

    def device_json(self, mac: str) -> dict | None:
        with self._lock:
            e = self._sensors.get(mac.lower())
            return None if e is None else self._entry_json(e, self.now_mono())

    def history_json(self, mac: str) -> dict | None:
        mac = mac.lower()
        now_wall = self.now_wall()
        now_minute = int(now_wall // 60)
        with self._lock:
            if mac not in self._sensors:
                return None
            hist = self._history.get(mac, {})
            points = sorted((m, t) for m, t in hist.items()
                            if now_minute - HISTORY_POINTS < m <= now_minute)
        return {"mac": mac, "interval_s": 60, "readings": [
            {"ts_ms": m * 60_000, "age_s": int(max(0, now_wall - m * 60)), "temperature_c": t}
            for m, t in points[-HISTORY_POINTS:]]}

    def info_json(self) -> dict:
        with self._lock:
            n = len(self._sensors)
        return {"proto": PROTO, "role": "hub", "device": FAKE_HUB_ID, "mac": self.mac,
                "fw": self.fw, "ip": self.address, "time_synced": True, "sensors": n}

    # ── HTTP ───────────────────────────────────────────────────

    def normal_response(self, method: str, path: str, body: bytes) -> tuple[int, object]:
        """The hub's own answer (sections 2 and 4), without rules."""
        parts = [p for p in path.split("/") if p]
        if parts == ["report"]:
            if method != "POST":
                return 405, {"error": "method not allowed"}
            return self.report(body)
        if parts == ["info.json"]:
            return (200, self.info_json()) if method == "GET" else (405, {"error": "method not allowed"})
        if parts == ["devices"]:
            return (200, self.devices_json()) if method == "GET" else (405, {"error": "method not allowed"})
        if len(parts) == 2 and parts[0] == "devices":
            if method == "GET":
                e = self.device_json(parts[1])
                return (200, e) if e is not None else (404, {"error": "unknown device"})
            if method == "DELETE":
                return (200, {"ok": True}) if self.forget(parts[1]) else \
                    (404, {"error": "unknown device"})
            return 405, {"error": "method not allowed"}
        if len(parts) == 3 and parts[0] == "devices" and parts[2] == "history":
            if method != "GET":
                return 405, {"error": "method not allowed"}
            h = self.history_json(parts[1])
            return (200, h) if h is not None else (404, {"error": "unknown device"})
        return 404, {"error": "not found"}

    def match_rule(self, method: str, path: str) -> Rule | None:
        with self._lock:
            return self._rules.get((method, path)) or self._rules.get(("ANY", path))

    def fill(self, body: str) -> str:
        return (body.replace("{device}", FAKE_HUB_ID).replace("{mac}", self.mac)
                .replace("{ip}", self.address)
                .replace("{ts_ms}", str(int(self.now_wall() * 1000))))


def _validate_report(r) -> str | None:
    if not isinstance(r, dict):
        return "body must be a JSON object"
    if "proto" in r and r["proto"] != PROTO:
        return f"unsupported proto {r['proto']!r}"
    mac = r.get("mac")
    if not isinstance(mac, str) or not MAC_RE.match(mac.lower()):
        return "bad or missing mac"
    for k in ("device", "ip", "fw"):
        if k in r and not isinstance(r[k], str):
            return f"{k} must be a string"
    iv = r.get("interval_s")
    if isinstance(iv, bool) or not isinstance(iv, int) or iv <= 0:
        return "bad or missing interval_s"
    if not isinstance(r.get("ok"), bool):
        return "bad or missing ok"
    if "temperature_c" not in r:
        return "missing temperature_c"
    t = r["temperature_c"]
    if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float))):
        return "temperature_c must be a number or null"
    if r["ok"] and t is None:
        return "temperature_c is null but ok is true"
    if "err" in r and (isinstance(r["err"], bool) or not isinstance(r["err"], int)):
        return "err must be an integer"
    return None


def _make_handler(hub: FakeHub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tshub-fake"
        timeout = SOCKET_TIMEOUT_S  # a stalled client frees its thread

        def log_message(self, fmt, *args):  # quiet
            pass

        def _read_body(self):
            """(body, None) or (None, (status, error)) for a body that can't be
            taken: chunked (411), a bad Content-Length (400), too big (413). The
            caller answers and closes the connection then, since the unread body
            would otherwise be parsed as the next request."""
            if self.headers.get("Transfer-Encoding"):
                return None, (411, "send a Content-Length (no chunked bodies)")
            raw = (self.headers.get("Content-Length") or "").strip()
            if not raw:
                return b"", None
            try:
                n = int(raw)
            except ValueError:
                return None, (400, "bad Content-Length")
            if n < 0:
                return None, (400, "bad Content-Length")
            if n > MAX_REQUEST_BYTES:
                return None, (413, f"body too large (max {MAX_REQUEST_BYTES} bytes)")
            return (self.rfile.read(n) if n else b""), None

        def _send(self, status: int, payload, raw: str | None = None) -> None:
            if raw is not None:
                body = raw.encode()
                ctype = ("application/json" if raw.lstrip()[:1] in ("{", "[")
                         else "text/plain")
            elif payload is None:
                body, ctype = b"", None
            else:
                body, ctype = json.dumps(payload).encode(), "application/json"
            self.send_response(status)
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _handle(self, method: str) -> None:
            path = urlsplit(self.path).path
            if len(path) > MAX_PATH_LEN:
                self.close_connection = True
                return self._send(414, {"error": f"path too long (max {MAX_PATH_LEN})"})
            body, problem = self._read_body()
            if problem is not None:
                self.close_connection = True
                return self._send(problem[0], {"error": problem[1]})
            try:
                rule = hub.match_rule(method, path)
                if rule is None:
                    status, obj = hub.normal_response(method, path, body)
                    return self._send(status, obj)
                if rule.kind == "hang":
                    rule.released.wait(HANG_S)
                    self.close_connection = True  # never answered
                    return
                if rule.kind == "delay":
                    rule.released.wait(rule.delay_ms / 1000.0)
                    if not hub.active:  # stopped meanwhile
                        self.close_connection = True
                        return
                if rule.kind in ("status", "delay"):
                    if rule.body is None:
                        return self._send(rule.status, None)
                    return self._send(rule.status, None, raw=hub.fill(rule.body))
                if rule.kind == "sequence":
                    value = rule.next_value()
                    status, obj = hub.normal_response(method, path, body)
                    _replace_temperatures(obj, value)
                    return self._send(status, obj)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

        def do_PUT(self):
            self._handle("PUT")

    return Handler
