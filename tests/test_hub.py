"""Roles (device=sensor|hub), the fake hub (hub-protocol.md sections 1-4 and the
hub rule language of section 6) and its mDNS advertisement.

Run: python3 -m unittest discover -s tests   (needs pyserial; zeroconf is mocked)
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

os.environ.setdefault("ESP32_READY_TIMEOUT_S", "3")
import fake_hub  # noqa: E402
import esp32_plugin  # noqa: E402
from esp32_plugin import Esp32Plugin  # noqa: E402
from fake_board import SENSOR_NO_HTTP  # noqa: E402
from fake_hub import FAKE_HUB_ID, FAKE_HUB_MAC, FakeHub, MdnsAdvertiser  # noqa: E402
from test_plugin import FakeAdvertiser, Harness  # noqa: E402

SENSOR = "7c:df:a1:a4:8f:00"   # /dev/fake0
HUB = "7c:df:a1:a4:8f:01"      # /dev/fake1
TWO = ("/dev/fake0", "/dev/fake1")


def report(mac="7c:df:a1:a4:8f:44", temp=21.5, ok=True, interval_s=3, **kw):
    r = {"proto": 1, "mac": mac, "device": "ts-" + mac.lower().replace(":", "")[4:],
         "ip": "192.168.125.6", "fw": "0.2.0", "interval_s": interval_s,
         "temperature_c": temp if ok else None, "ok": ok, "err": 0 if ok else 3}
    r.update(kw)
    return r


class Clock:
    """Controls the fake hub's wall clock (epoch s) and monotonic clock."""

    def __init__(self, hub, wall=1_793_919_600.0):  # a whole minute
        self.wall, self.mono = wall, 1000.0
        hub.now_wall = lambda: self.wall
        hub.now_mono = lambda: self.mono

    def advance(self, s):
        self.wall += s
        self.mono += s


class HubClient:
    def __init__(self, hub):
        self.hub = hub

    def call(self, method, path, body=None, raw=None, timeout=5):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=timeout)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"} if data is not None else {}
        try:
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            text = r.read().decode()
        finally:
            conn.close()
        ctype = r.getheader("Content-Type")
        val = json.loads(text) if ctype == "application/json" and text else text
        return r.status, val, ctype

    def get(self, path):
        code, val, _ = self.call("GET", path)
        return code, val

    def post_report(self, **kw):
        code, val, _ = self.call("POST", "/report", report(**kw))
        return code, val


def start_hub(test):
    FakeAdvertiser.instances = []
    hub = FakeHub(port=0, bind="127.0.0.1", address="127.0.0.1", fw="9.9.9",
                  advertiser_factory=FakeAdvertiser)
    hub.start()
    test.addCleanup(hub.stop)
    return hub


# ───────────────────────────── roles ─────────────────────────────


class RoleTests(unittest.TestCase):
    def harness(self, **kw):
        h = Harness(**kw)
        self.addCleanup(h.close)
        return h

    def test_hub_is_the_default_and_sensor_boards_are_not_ours(self):
        h = self.harness(ports=TWO, roles=("sensor", "hub"), fake_hub="auto")
        self.assertEqual(h.run("clear")["device"], HUB)
        self.assertEqual(h.run("clear", device="hub")["device"], HUB)
        self.assertEqual(h.run("clear", device="server")["device"], HUB)
        self.assertEqual(h.run("clear", device=HUB)["device"], HUB)
        code, r = h.call("POST", "/jobs", {"action": "clear", "device": "sensor"})
        self.assertEqual((code, r["code"]), (404, "wrong_plugin"))
        # the sensor board was probed, dropped and its role cached: only the
        # hub board is ours; a real hub is attached: no fake hub, nothing
        # advertised
        code, d = h.call("GET", "/devices")
        self.assertEqual({x["id"]: x["details"]["role"] for x in d["devices"]},
                         {HUB: "hub"})
        self.assertFalse(h.plugin.fake_hub.active)
        self.assertEqual(FakeAdvertiser.instances, [])

    def test_hub_board_reset_by_the_port_open_is_kept(self):
        """Opening a CP2102N port resets the board: the discovery `status` goes
        out while it boots and is lost. Its `@vt - ready` line (role hub) must
        identify it; dropping it closed the port, the next rescan reopened it
        (another reset) and the board looped every rescan with the fake hub
        standing in."""
        import fake_board
        orig_init = fake_board.FakeSerial.__init__

        def resetting_init(ser, firmware, *a, **kw):
            orig_init(ser, firmware, *a, **kw)
            firmware.silent = True          # booting: requests are lost
            firmware.boot_delay = 0.4

            def boot_done():
                time.sleep(0.6)             # after the ready line went out
                firmware.silent = False
            firmware.boot()
            threading.Thread(target=boot_done, daemon=True).start()

        fake_board.FakeSerial.__init__ = resetting_init
        self.addCleanup(setattr, fake_board.FakeSerial, "__init__", orig_init)
        h = self.harness(roles=("hub",), fake_hub="auto")
        # past the lost probe's 1.5 s timeout: before the fix the board was
        # dropped right there
        time.sleep(2.5)
        code, d = h.call("GET", "/devices")
        self.assertEqual([(x["id"], x["details"]["role"]) for x in d["devices"]],
                         [("7c:df:a1:a4:8f:00", "hub")])
        self.assertFalse(h.plugin.fake_hub.active)
        fw = h.fws["/dev/fake0"]
        self.assertEqual(fw.reboots, 1)     # opened once, never reopened
        self.assertEqual(h.opens["/dev/fake0"], 1)

    def test_board_that_never_says_ready_is_still_dropped(self):
        h = self.harness(roles=("hub",), vt=False)
        time.sleep(esp32_plugin.IDENTIFY_READY_WAIT_S + 2)
        code, d = h.call("GET", "/devices")
        self.assertEqual(d["devices"], [])

    def test_firmware_without_role_counts_as_a_sensor_and_is_dropped(self):
        h = self.harness(roles=(None,))
        code, d = h.call("GET", "/devices")
        self.assertEqual(d["devices"], [])

    def test_hub_needs_exactly_one_hub_board(self):
        h = Harness(ports=TWO, roles=("hub", "hub"), fake_hub="off")
        try:
            code, r = h.call("POST", "/jobs", {"action": "clear"})
            self.assertEqual((code, r["code"]), (400, "bad_request"))
            self.assertIn("2 hub boards attached", r["error"])
        finally:
            h.close()

    def test_hub_resolves_to_the_fake_hub_when_no_hub_board(self):
        h = self.harness(roles=("sensor",), fake_hub="auto")
        job = h.run("clear", device="hub")
        self.assertEqual((job["state"], job["device"]), ("passed", FAKE_HUB_ID))
        self.assertEqual(h.run("clear", device=FAKE_HUB_MAC)["device"], FAKE_HUB_ID)
        code, d = h.call("GET", "/devices")
        fake = [x for x in d["devices"] if x["id"] == FAKE_HUB_ID][0]
        self.assertEqual(fake["details"]["role"], "hub")
        self.assertTrue(fake["details"]["fake"])
        self.assertEqual(fake["details"]["url"], f"http://127.0.0.1:{h.plugin.fake_hub.port}")
        self.assertEqual(fake["details"]["mdns"], "advertising")
        self.assertEqual(len(FakeAdvertiser.instances), 1)
        self.assertTrue(FakeAdvertiser.instances[0].started)

    def test_no_boards_at_all_still_gives_the_fake_hub(self):
        h = self.harness(ports=(), fake_hub="auto")
        self.assertEqual(h.run("clear", device="hub")["device"], FAKE_HUB_ID)
        # the default is the hub too: omitted `device=` is the fake server
        self.assertEqual(h.run("clear")["device"], FAKE_HUB_ID)

    def test_fake_hub_on_wins_over_a_hub_board(self):
        os.environ["ESP32_FAKE_HUB"] = "on"
        self.addCleanup(os.environ.pop, "ESP32_FAKE_HUB", None)
        h = self.harness(ports=TWO, roles=("sensor", "hub"), fake_hub=None)
        self.assertEqual(h.plugin.fake_hub_mode, "on")
        self.assertEqual(h.run("clear", device="hub")["device"], FAKE_HUB_ID)
        self.assertEqual(h.run("clear", device=HUB)["device"], HUB)  # still reachable by MAC

    def test_fake_hub_off(self):
        os.environ["ESP32_FAKE_HUB"] = "off"
        self.addCleanup(os.environ.pop, "ESP32_FAKE_HUB", None)
        h = self.harness(roles=("sensor",), fake_hub=None)
        self.assertIsNone(h.plugin.fake_hub)
        code, r = h.call("POST", "/jobs", {"action": "clear", "device": "hub"})
        self.assertEqual((code, r["code"]), (404, "unknown_device"))
        self.assertIn("ESP32_FAKE_HUB=off", r["error"])
        code, d = h.call("GET", "/devices")
        self.assertEqual(d["devices"], [])  # sensor boards belong to esp32-sensor

    def test_bad_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            Esp32Plugin(ports=[], rescan=False, fake_hub_mode="maybe", log=lambda m: None)

    def test_sensor_board_is_probed_dropped_and_cached(self):
        """The ownership split, server side: a sensor node is probed, dropped
        and its role cached under node path and MAC, so the next scan skips
        the node without touching its console."""
        cache = os.path.join(tempfile.mkdtemp(), "roles.json")
        h = self.harness(ports=TWO, roles=("sensor", "hub"), role_cache=cache)
        time.sleep(0.3)  # the identify thread finishes the probe+drop+cache
        code, d = h.call("GET", "/devices")
        self.assertEqual({x["id"]: x["details"]["role"] for x in d["devices"]},
                         {HUB: "hub"})
        with open(cache) as f:
            cached = json.load(f)
        self.assertEqual(cached.get(os.path.realpath("/dev/fake0")), "sensor")
        self.assertEqual(cached.get(SENSOR), "sensor")
        self.assertEqual(cached.get(HUB), "hub")

    def test_fake_hub_port_in_use_is_reported(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        self.addCleanup(s.close)
        plugin = Esp32Plugin(ports=[], rescan=False, fake_hub_mode="auto",
                             fake_hub_port=s.getsockname()[1], fake_hub_bind="127.0.0.1",
                             fake_hub_address="127.0.0.1", mdns_advertiser=FakeAdvertiser,
                             log=lambda m: None)
        self.addCleanup(plugin.close)
        self.assertFalse(plugin.fake_hub.active)
        self.assertIn("fake hub: can't listen", plugin.device_error())
        with self.assertRaises(Exception) as cm:
            plugin.resolve_device("hub")
        self.assertIn("isn't running", str(cm.exception))


class FakeHubActionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(roles=("sensor",), fake_hub="auto", transport="ble")
        self.addCleanup(self.h.close)
        self.hub = self.h.plugin.fake_hub
        self.client = HubClient(self.hub)

    def test_board_only_actions_fail_on_the_fake_hub(self):
        for action, params in (("flash", {"firmware_dir": "nonexistent"}), ("reset", {})):
            job = self.h.run(action, params, device="hub")
            self.assertEqual(job["state"], "error", job)
            self.assertEqual(job["error"]["code"], "unsupported")
            self.assertIn(f"{action} is not supported on the fake hub", job["error"]["message"])

    def test_expect_accumulates_and_reports_the_rejected_line(self):
        job = self.h.run("expect", {"script": "GET /devices -> 500\nGET /info.json -> hang"},
                         device="hub")
        self.assertEqual(job["result"], {"rules": 2, "applied": 2})
        job = self.h.run("expect", {"script": "# only /devices changes\n"
                                              "GET /devices -> 503\n"
                                              "GET /x -> 200 {\"a\":1}\n"
                                              "GET /y => 500\n"
                                              "GET /z -> 500"}, device="hub")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "rule_rejected")
        self.assertEqual((job["result"]["line"], job["result"]["applied"]), (3, 2))
        self.assertIn("GET /y => 500", job["error"]["message"])
        self.assertEqual(self.hub.rules(), {"GET /devices": "503", "GET /info.json": "hang",
                                            "GET /x": '200 {"a":1}'})
        job = self.h.run("expect", {"script": "GET /x -> normal"}, device="hub")
        self.assertEqual(job["result"]["rules"], 2)

    def test_clear_factory_reset_and_forget(self):
        self.client.post_report(mac="aa:00:00:00:00:01")
        self.client.post_report(mac="aa:00:00:00:00:02")
        self.h.run("expect", {"script": "GET /devices -> 500"}, device="hub")
        self.assertEqual(self.h.run("clear", device="hub")["result"], {"rules": 0})
        self.assertEqual(self.client.get("/devices")[0], 200)
        job = self.h.run("forget", {"mac": "AA:00:00:00:00:01"}, device="hub")
        self.assertEqual(job["result"], {"forgotten": "aa:00:00:00:00:01", "sensors": 1})
        job = self.h.run("forget", {"mac": "aa:00:00:00:00:01"}, device="hub")
        self.assertEqual((job["state"], job["error"]["code"]), ("failed", "device_error"))
        job = self.h.run("forget", {"mac": "nope"}, device="hub")
        self.assertEqual((job["state"], job["error"]["code"]), ("error", "bad_request"))
        self.h.run("expect", {"script": "GET /devices -> 500"}, device="hub")
        job = self.h.run("factory-reset", device="hub")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual((job["result"]["status"]["rules"], job["result"]["status"]["sensors"]),
                         (0, 0))
        self.assertEqual(self.client.get("/devices"), (200, []))
        self.assertEqual(self.client.get("/devices/aa:00:00:00:00:02/history")[0], 404)

    def test_spec_section_7_example(self):
        """hub-protocol.md section 7 (server side): the sensor provisioning
        happens at the esp32-sensor plugin; here the hub is mocked and the
        sensor's report lands in its registry."""
        self.assertEqual(self.h.run("factory-reset", device="hub")["state"], "passed")
        # …the app provisioned the sensor via the other plugin; it reports…
        self.assertEqual(self.client.post_report(mac=SENSOR, temp=22.0), (200, {"ok": True}))
        job = self.h.run("expect", {"script": "  GET /devices -> sequence 17 18 19\n"},
                         device="hub")
        self.assertEqual(job["state"], "passed", job)
        temps = [self.client.get("/devices")[1][0]["temperature_c"] for _ in range(4)]
        self.assertEqual(temps, [17, 18, 19, 17])


class RealHubAndSensorTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(ports=TWO, roles=("sensor", "hub"), fake_hub="auto", transport="ble")
        self.addCleanup(self.h.close)
        self.sensor, self.hubfw = self.h.fws["/dev/fake0"], self.h.fws["/dev/fake1"]

    def sent(self, fw):
        return [ln.split(" ", 2)[2] for ln in fw.received if ln.startswith("@vt p")]

    def test_expect_on_the_hub_board_relays_rule_lines(self):
        job = self.h.run("expect", {"script": "GET /devices -> sequence 17 18\nPOST /report -> 500"},
                         device="hub")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(self.sent(self.hubfw)[-2:], ["rule GET /devices -> sequence 17 18",
                                                      "rule POST /report -> 500"])
        self.assertEqual(self.h.run("clear", device="hub")["result"], {"rules": 0})
        self.assertEqual(self.sent(self.sensor), ["status"])  # the sensor got nothing

    def test_forget_and_factory_reset_on_the_hub_board(self):
        self.hubfw.sensors = {"aa:00:00:00:00:01", "aa:00:00:00:00:02"}
        job = self.h.run("forget", {"mac": "aa:00:00:00:00:01"}, device="hub")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(self.sent(self.hubfw)[-1], "forget aa:00:00:00:00:01")
        job = self.h.run("forget", {"mac": "aa:00:00:00:00:01"}, device="hub")
        self.assertEqual((job["state"], job["error"]["code"]), ("failed", "device_error"))
        job = self.h.run("factory-reset", device="hub")
        self.assertEqual(job["state"], "passed", job)
        self.assertEqual(job["result"]["status"]["role"], "hub")
        self.assertEqual(self.hubfw.sensors, set())

    def test_sensor_actions_are_not_exposed_here(self):
        """The provisioning actions live in the esp32-sensor plugin."""
        for action in ("ble-advertise", "broadcast-ssid"):
            code, r = self.h.call("POST", "/jobs", {"action": action, "device": "hub"})
            self.assertEqual((code, r["code"]), (404, "unknown_action"))

class FakeHubApiTests(unittest.TestCase):
    def setUp(self):
        self.hub = start_hub(self)
        self.clock = Clock(self.hub)
        self.c = HubClient(self.hub)

    def test_info(self):
        self.c.post_report()
        self.assertEqual(self.c.get("/info.json"), (200, {
            "proto": 1, "role": "hub", "device": "tshub-fake", "mac": FAKE_HUB_MAC,
            "fw": "9.9.9", "ip": "127.0.0.1", "time_synced": True, "sensors": 1}))

    def test_report_registers_and_devices_lists_sorted(self):
        self.assertEqual(self.c.get("/devices"), (200, []))
        self.assertEqual(self.c.post_report(mac="7C:DF:A1:A4:8F:44", temp=21.5), (200, {"ok": True}))
        self.clock.advance(2)
        self.c.post_report(mac="00:11:22:33:44:55", ok=False)
        code, devs = self.c.get("/devices")
        self.assertEqual([d["mac"] for d in devs], ["00:11:22:33:44:55", "7c:df:a1:a4:8f:44"])
        self.assertEqual(devs[1], {
            "mac": "7c:df:a1:a4:8f:44", "device": "ts-a1a48f44", "ip": "192.168.125.6",
            "fw": "0.2.0", "online": True, "age_s": 2, "last_seen_ms": 1_793_919_600_000,
            "temperature_c": 21.5, "ok": True, "err": 0})
        self.assertEqual((devs[0]["temperature_c"], devs[0]["ok"], devs[0]["err"]), (None, False, 3))
        self.assertEqual(self.c.get("/devices/7c:df:a1:a4:8f:44"), (200, devs[1]))
        self.assertEqual(self.c.get("/devices/7C:DF:A1:A4:8F:44")[0], 200)
        self.assertEqual(self.c.get("/devices/aa:aa:aa:aa:aa:aa"),
                         (404, {"error": "unknown device"}))
        self.assertEqual(self.c.get("/nope")[0], 404)
        self.assertEqual(self.c.call("GET", "/report")[0], 405)

    def test_malformed_reports_are_400_and_store_nothing(self):
        for raw in (b"not json", b"[1]", json.dumps(report(mac="zz")).encode(),
                    json.dumps(report(interval_s=0)).encode(),
                    json.dumps(report(proto=2)).encode(),
                    json.dumps({k: v for k, v in report().items() if k != "ok"}).encode(),
                    json.dumps(report(temperature_c="hot")).encode(),
                    json.dumps(report(fw="x" * 600)).encode()):
            code, body, _ = self.c.call("POST", "/report", raw=raw)
            self.assertEqual(code, 400, raw[:40])
            self.assertIn("error", body)
        self.assertEqual(self.c.get("/devices"), (200, []))

    def test_online_rule(self):
        self.c.post_report(mac="aa:00:00:00:00:01", interval_s=3)   # online for 15 s
        self.c.post_report(mac="aa:00:00:00:00:02", interval_s=10)  # online for 30 s
        self.clock.advance(14.9)
        self.assertEqual([d["online"] for d in self.c.get("/devices")[1]], [True, True])
        self.clock.advance(0.2)
        devs = self.c.get("/devices")[1]
        self.assertEqual([d["online"] for d in devs], [False, True])
        self.assertEqual(devs[0]["age_s"], 15)
        self.clock.advance(15)
        self.assertEqual([d["online"] for d in self.c.get("/devices")[1]], [False, False])
        self.c.post_report(mac="aa:00:00:00:00:01")
        self.assertTrue(self.c.get("/devices/aa:00:00:00:00:01")[1]["online"])

    def test_history_buckets_per_minute(self):
        mac = "aa:00:00:00:00:01"
        m0 = int(self.clock.wall // 60) * 60
        self.clock.advance(5)
        self.c.post_report(mac=mac, temp=20.0)
        self.clock.advance(25)
        self.c.post_report(mac=mac, temp=21.0)      # same minute: last good reading wins
        self.clock.advance(10)
        self.c.post_report(mac=mac, ok=False)       # a failed read changes nothing
        self.clock.advance(60)                      # minute 1: no report -> gap
        self.clock.advance(30)                      # minute 2 (+130 s)
        self.c.post_report(mac=mac, temp=22.5)
        code, h = self.c.get(f"/devices/{mac}/history")
        self.assertEqual((code, h["mac"], h["interval_s"]), (200, mac, 60))
        self.assertEqual(h["readings"], [
            {"ts_ms": m0 * 1000, "age_s": 130, "temperature_c": 21.0},
            {"ts_ms": (m0 + 120) * 1000, "age_s": 10, "temperature_c": 22.5}])
        self.assertEqual(self.c.get("/devices/aa:aa:aa:aa:aa:aa/history")[0], 404)

    def test_history_keeps_the_last_180_minutes(self):
        mac = "aa:00:00:00:00:01"
        for i in range(200):
            self.c.post_report(mac=mac, temp=float(i % 100))
            self.clock.advance(60)
        h = self.c.get(f"/devices/{mac}/history")[1]["readings"]
        self.assertEqual(len(h), 179)  # the current minute has no reading yet
        self.c.post_report(mac=mac, temp=50.0)
        h = self.c.get(f"/devices/{mac}/history")[1]["readings"]
        self.assertEqual(len(h), 180)
        self.assertEqual([r["ts_ms"] for r in h], sorted(r["ts_ms"] for r in h))
        self.assertEqual(h[-1]["temperature_c"], 50.0)
        self.assertEqual(h[0]["temperature_c"], 21.0)  # report #21 is 179 minutes old
        self.clock.advance(170 * 60)  # old points age out even without new reports
        self.assertEqual(len(self.c.get(f"/devices/{mac}/history")[1]["readings"]), 10)

    def test_registry_eviction_and_507(self):
        macs = [f"aa:00:00:00:00:{i:02x}" for i in range(16)]
        for m in macs:
            self.assertEqual(self.c.post_report(mac=m)[0], 200)
        self.assertEqual(self.c.post_report(mac="bb:00:00:00:00:00"),
                         (507, {"error": "registry full"}))
        self.clock.advance(20)  # all offline now
        self.c.post_report(mac=macs[0])
        self.clock.advance(1)
        for m in macs[2:]:      # macs[1] stays offline the longest
            self.c.post_report(mac=m)
        self.assertEqual(self.c.post_report(mac="bb:00:00:00:00:00")[0], 200)
        known = [d["mac"] for d in self.c.get("/devices")[1]]
        self.assertEqual(len(known), 16)
        self.assertNotIn(macs[1], known)
        self.assertIn("bb:00:00:00:00:00", known)
        self.assertEqual(self.c.get("/info.json")[1]["sensors"], 16)

    def test_delete(self):
        mac = "aa:00:00:00:00:01"
        self.c.post_report(mac=mac)
        self.assertEqual(self.c.call("DELETE", f"/devices/{mac}")[:2], (200, {"ok": True}))
        self.assertEqual(self.c.call("DELETE", f"/devices/{mac}")[:2],
                         (404, {"error": "unknown device"}))
        self.assertEqual(self.c.get(f"/devices/{mac}/history")[0], 404)
        self.c.post_report(mac=mac)  # a running sensor re-registers
        self.assertEqual(len(self.c.get(f"/devices/{mac}/history")[1]["readings"]), 1)

    def test_epoch_ms_from_the_host_clock(self):
        hub = start_hub(self)  # real clocks
        HubClient(hub).post_report()
        seen = HubClient(hub).get("/devices")[1][0]["last_seen_ms"]
        self.assertLess(abs(seen - time.time() * 1000), 5000)


# ───────────────────────────── hub rule language ─────────────────────────────


class FakeHubRuleTests(unittest.TestCase):
    def setUp(self):
        self.hub = start_hub(self)
        self.c = HubClient(self.hub)

    def test_status_and_body_with_placeholders(self):
        self.hub.add_rule('GET /info.json -> 200 {"device":"{device}","mac":"{mac}","ip":"{ip}",'
                          '"ts":{ts_ms}}')
        code, body, ctype = self.c.call("GET", "/info.json")
        self.assertEqual((code, ctype), (200, "application/json"))
        self.assertEqual((body["device"], body["mac"], body["ip"]),
                         ("tshub-fake", FAKE_HUB_MAC, "127.0.0.1"))
        self.assertLess(abs(body["ts"] - time.time() * 1000), 5000)
        self.hub.add_rule("GET /devices -> 503 hub busy")
        self.assertEqual(self.c.call("GET", "/devices"), (503, "hub busy", "text/plain"))
        self.hub.add_rule("GET /devices -> 204")
        self.assertEqual(self.c.call("GET", "/devices")[:2], (204, ""))
        self.hub.add_rule("GET /anything -> 200 [1]")  # any path, also unserved ones
        self.assertEqual(self.c.get("/anything"), (200, [1]))

    def test_post_report_rule_refuses_sensors(self):
        self.hub.add_rule("POST /report -> 500")
        self.assertEqual(self.c.post_report()[0], 500)
        self.assertEqual(self.c.get("/devices"), (200, []))
        self.hub.add_rule("POST /report -> normal")
        self.assertEqual(self.c.post_report()[0], 200)

    def test_method_rule_wins_over_any(self):
        self.hub.add_rule("ANY /devices -> 500")
        self.assertEqual(self.c.get("/devices")[0], 500)
        self.hub.add_rule("GET /devices -> 418")
        self.assertEqual(self.c.get("/devices")[0], 418)
        self.hub.add_rule("GET /devices -> normal")
        self.assertEqual(self.c.get("/devices")[0], 500)
        self.hub.add_rule("ANY /devices/aa:00:00:00:00:01 -> 409")  # ANY covers DELETE too
        self.assertEqual(self.c.call("DELETE", "/devices/aa:00:00:00:00:01")[0], 409)

    def test_delay(self):
        self.hub.add_rule("GET /devices -> delay 300 200 []")
        t = time.monotonic()
        self.assertEqual(self.c.get("/devices"), (200, []))
        self.assertGreaterEqual(time.monotonic() - t, 0.3)

    def test_hang_until_the_rule_is_removed(self):
        self.hub.add_rule("GET /devices -> hang")
        with self.assertRaises((socket.timeout, TimeoutError)):
            self.c.call("GET", "/devices", timeout=0.5)
        self.assertEqual(self.c.get("/info.json")[0], 200)  # other requests are served
        out = {}

        def held():
            t = time.monotonic()
            try:
                self.c.call("GET", "/devices", timeout=10)
                out["result"] = "answered"
            except (http.client.RemoteDisconnected, ConnectionError) as e:
                out["result"] = type(e).__name__
            out["s"] = time.monotonic() - t

        th = threading.Thread(target=held)
        th.start()
        time.sleep(0.3)
        self.hub.add_rule("GET /devices -> normal")
        th.join(5)
        self.assertEqual(out["result"], "RemoteDisconnected")  # closed, never answered
        self.assertLess(out["s"], 3)
        self.assertEqual(self.c.get("/devices"), (200, []))

    def test_sequence_replaces_every_temperature(self):
        self.c.post_report(mac="aa:00:00:00:00:01", temp=30.0)
        self.c.post_report(mac="aa:00:00:00:00:02", ok=False)
        self.hub.add_rule("GET /devices -> sequence 17 18.5")
        seen = []
        for _ in range(3):
            devs = self.c.get("/devices")[1]
            seen.append([(d["temperature_c"], d["ok"], d["err"]) for d in devs])
        self.assertEqual(seen, [[(17.0, True, 0)] * 2, [(18.5, True, 0)] * 2,
                                [(17.0, True, 0)] * 2])
        # only requests the rule answers move it on; setting it again starts over
        self.assertEqual(self.c.get("/devices/aa:00:00:00:00:01")[1]["temperature_c"], 30.0)
        self.hub.add_rule("GET /devices -> sequence 17 18.5")
        self.assertEqual(self.c.get("/devices")[1][0]["temperature_c"], 17.0)
        # history and a single device
        self.hub.add_rule("GET /devices/aa:00:00:00:00:01/history -> sequence -5.5")
        h = self.c.get("/devices/aa:00:00:00:00:01/history")[1]
        self.assertEqual([r["temperature_c"] for r in h["readings"]], [-5.5])
        self.hub.add_rule("GET /devices/aa:00:00:00:00:01 -> sequence 125")
        self.assertEqual(self.c.get("/devices/aa:00:00:00:00:01")[1]["temperature_c"], 125.0)
        # an unknown device stays a 404 under a sequence rule
        self.hub.add_rule("GET /devices/aa:00:00:00:00:09 -> sequence 1")
        self.assertEqual(self.c.get("/devices/aa:00:00:00:00:09")[0], 404)

    def test_normal_clear_and_limits(self):
        for i in range(16):
            self.assertEqual(self.hub.add_rule(f"GET /p{i} -> 500"), i + 1)
        with self.assertRaisesRegex(ValueError, "too many rules"):
            self.hub.add_rule("GET /p16 -> 500")
        self.assertEqual(self.hub.add_rule("GET /p0 -> 404"), 16)  # replacing is fine
        self.assertEqual(self.hub.add_rule("GET /p0 -> normal"), 15)
        self.assertEqual(self.hub.add_rule("GET /never-set -> normal"), 15)
        self.hub.clear()
        self.assertEqual(self.hub.status()["rules"], 0)
        self.hub.add_rule("GET /a -> 200 " + "x" * 200)
        with self.assertRaisesRegex(ValueError, "body too long"):
            self.hub.add_rule("GET /a -> 200 " + "x" * 201)

    def test_bad_lines(self):
        for line, msg in (("GET /a => 500", "expected"), ("PUT /a -> 500", "bad method"),
                          ("GET a -> 500", "bad path"), ("GET /a -> 99", "bad status"),
                          ("GET /a -> 600", "bad status"), ("GET /a -> teapot", "bad status"),
                          ("GET /a -> delay 70000 200", "delay"),
                          ("GET /a -> delay 10", "delay"),
                          ("GET /a -> sequence", "bad sequence"),
                          ("GET /a -> sequence 1 2 3 4 5 6 7 8 9", "bad sequence"),
                          ("GET /a -> sequence 126", "bad sequence value"),
                          ("GET /a -> sequence 1e2", "bad sequence value"),
                          ("GET /a -> hang 5", "no arguments"),
                          ("wifi off", "not a hub directive"),
                          ("report off", "not a hub directive"),
                          ("temp 17", "not a hub directive")):
            with self.assertRaisesRegex(ValueError, msg, msg=line):
                self.hub.add_rule(line)
        self.assertEqual(self.hub.status()["rules"], 0)


# ───────────────────────────── mDNS ─────────────────────────────


class MdnsTests(unittest.TestCase):
    def test_start_stop_advertises_tshub_fake(self):
        hub = start_hub(self)
        adv = FakeAdvertiser.instances[-1]
        self.assertEqual(adv.kw, {"instance": "tshub-fake", "port": hub.port,
                                  "txt": {"proto": "1", "mac": FAKE_HUB_MAC, "fw": "9.9.9"},
                                  "address": "127.0.0.1"})
        end = time.time() + 2
        while hub.mdns_state != "advertising" and time.time() < end:
            time.sleep(0.01)
        self.assertTrue(adv.started)
        self.assertEqual(hub.status()["mdns"], "advertising")
        hub.stop()
        self.assertTrue(adv.stopped)
        self.assertEqual(hub.status()["mdns"], "off")
        with self.assertRaises(OSError):
            HubClient(hub).get("/devices")

    def test_advertising_failure_keeps_http_up(self):
        class Broken(FakeAdvertiser):
            def start(self):
                raise RuntimeError("no multicast")

        hub = FakeHub(port=0, bind="127.0.0.1", address="127.0.0.1", advertiser_factory=Broken)
        hub.start()
        self.addCleanup(hub.stop)
        end = time.time() + 2
        while hub.mdns_state == "starting" and time.time() < end:
            time.sleep(0.01)
        self.assertIn("no multicast", hub.mdns_state)
        self.assertEqual(HubClient(hub).get("/devices"), (200, []))

    def test_mdns_advertiser_uses_zeroconf(self):
        calls = []

        class ServiceInfo:
            def __init__(self, type_, name, **kw):
                calls.append(("info", type_, name, kw))

        class Zeroconf:
            def __init__(self, **kw):
                calls.append(("zc", kw))

            def register_service(self, info):
                calls.append(("register",))

            def unregister_service(self, info):
                calls.append(("unregister",))

            def close(self):
                calls.append(("close",))

        mod = types.SimpleNamespace(ServiceInfo=ServiceInfo, Zeroconf=Zeroconf,
                                    IPVersion=types.SimpleNamespace(V4Only="v4"))
        saved = sys.modules.get("zeroconf")
        sys.modules["zeroconf"] = mod
        try:
            adv = MdnsAdvertiser("tshub-fake", 8092, {"proto": "1", "mac": FAKE_HUB_MAC,
                                                      "fw": "0.1.0"}, "192.168.125.70")
            adv.start()
            adv.stop()
        finally:
            if saved is None:
                sys.modules.pop("zeroconf", None)
            else:
                sys.modules["zeroconf"] = saved
        self.assertEqual(calls[0], ("info", "_tsserver._tcp.local.",
                                    "tshub-fake._tsserver._tcp.local.",
                                    {"addresses": [socket.inet_aton("192.168.125.70")],
                                     "port": 8092, "server": "tshub-fake.local.",
                                     "properties": {"proto": "1", "mac": FAKE_HUB_MAC,
                                                    "fw": "0.1.0"}}))
        self.assertEqual(calls[1], ("zc", {"interfaces": ["192.168.125.70"], "ip_version": "v4"}))
        self.assertEqual([c[0] for c in calls[2:]], ["register", "unregister", "close"])


class RealFlockTests(unittest.TestCase):
    """The console lock against a second, real process (not simulated): the
    sibling plugin must not get a node this process holds, and gets it once
    released. Guards against a lock call that silently does nothing."""

    def other_process_try(self, lock_dir, port):
        code = ("import sys; sys.path.insert(0, %r); import esp32_plugin as p; "
                "r = p.try_lock_port(%r, %r); print('none' if r is None else 'got')"
                % (os.path.dirname(HERE), lock_dir, port))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_a_held_node_is_refused_to_another_process(self):
        lock_dir = tempfile.mkdtemp()
        fd = esp32_plugin.try_lock_port(lock_dir, "/dev/null")
        self.assertNotIn(fd, (None, False))
        try:
            self.assertEqual(self.other_process_try(lock_dir, "/dev/null"), "none")
        finally:
            os.close(fd)
        self.assertEqual(self.other_process_try(lock_dir, "/dev/null"), "got")


# ───────────────────────────── fake hub hardening ─────────────────────────────


class FakeHubLimitsTests(unittest.TestCase):
    """The fake hub stays LAN-reachable (the sensor board connects over Wi-Fi),
    so it caps what it accepts and can reach nothing but its in-memory state."""

    def setUp(self):
        self.hub = start_hub(self)
        self.c = HubClient(self.hub)

    def raw(self, method, path, headers, body=b""):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders(body or None)
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            conn.close()

    def test_default_bind_is_lan_on_purpose(self):
        import fake_hub
        self.assertEqual(fake_hub.FAKE_HUB_DEFAULT_BIND, "0.0.0.0")  # nosec B104

    def test_oversize_bad_length_chunked_and_long_path(self):
        import fake_hub
        code, r = self.raw("POST", "/report",
                           {"Content-Length": str(fake_hub.MAX_REQUEST_BYTES + 1)})
        self.assertEqual(code, 413)
        self.assertEqual(self.raw("POST", "/report", {"Content-Length": "x"})[0], 400)
        self.assertEqual(self.raw("POST", "/report", {"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.raw("POST", "/report", {"Transfer-Encoding": "chunked"},
                                  b"0\r\n\r\n")[0], 411)
        self.assertEqual(self.c.get("/" + "a" * (fake_hub.MAX_PATH_LEN + 1))[0], 414)
        # still serving normally afterwards
        self.assertEqual(self.c.post_report(), (200, {"ok": True}))

    def test_bad_reports_are_400(self):
        for raw in (b"{", b"\xff", b"[]", json.dumps({"mac": "../../etc"}).encode()):
            self.assertEqual(self.c.call("POST", "/report", raw=raw)[0], 400, raw)

    def test_no_file_or_process_access(self):
        import ast
        import fake_hub
        with open(fake_hub.__file__) as f:
            tree = ast.parse(f.read())
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module.split(".")[0])
        for banned in ("subprocess", "os", "shutil", "pathlib", "esp32_plugin", "board"):
            self.assertNotIn(banned, imported)
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for banned in ("open", "exec", "eval", "__import__"):
            self.assertNotIn(banned, calls)


if __name__ == "__main__":
    unittest.main()
