"""Security hardening tests: bind/token policy, request caps, path traversal,
upload limits and input validators. Shared VERBATIM by MonkeyTester-Plugin-Esp32-Sensor
and MonkeyTester-Plugin-Esp32-Server (both plugins must accept/refuse the same things).

No hardware: boards are tests/fake_board.py; the startup tests run
esp32_plugin.py with ESP32_PORTS pointing at a path that doesn't exist.

Run: python3 -m unittest discover -s tests   (needs pyserial importable)
"""

import contextlib
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

os.environ.setdefault("ESP32_READY_TIMEOUT_S", "3")
import esp32_plugin  # noqa: E402
import plugin_server as ps  # noqa: E402
import security  # noqa: E402
from plugin_server import ActionError, PluginServer  # noqa: E402
from test_plugin import Harness, make_result_build  # noqa: E402

TOKEN = "0123456789abcdef-test-token"


def scrubbed_env(**extra):
    """A child-process env with no real ports, no fake hub and private paths."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PLUGIN_", "ESP32_"))}
    tmp = tempfile.mkdtemp()
    env.update({"ESP32_PORTS": os.path.join(tmp, "no-such-port"), "ESP32_FAKE_HUB": "off",
                "ESP32_LOCK_DIR": os.path.join(tmp, "locks"),
                "ESP32_ROLE_CACHE": os.path.join(tmp, "roles.json"),
                "PLUGIN_UPLOAD_DIR": os.path.join(tmp, "uploads"), "PLUGIN_PORT": "0"})
    env.update(extra)
    return env


class BindPolicyTests(unittest.TestCase):
    def test_loopback_detection(self):
        for h in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"):
            self.assertTrue(security.is_loopback(h), h)
        for h in ("0.0.0.0", "", None, "::", "192.168.125.70", "example.com"):  # nosec B104
            self.assertFalse(security.is_loopback(h), h)

    def test_defaults_are_loopback_without_token(self):
        self.assertEqual(security.DEFAULT_BIND, "127.0.0.1")
        self.assertEqual(security.bind_and_token_from_env({}), ("127.0.0.1", None))
        self.assertEqual(security.bind_and_token_from_env({"PLUGIN_BIND": " ", "PLUGIN_TOKEN": " "}),
                         ("127.0.0.1", None))

    def test_policy(self):
        self.assertIsNone(security.bind_policy_error("127.0.0.1", None))
        self.assertIsNone(security.bind_policy_error("::1", "short"))
        self.assertIn("PLUGIN_TOKEN", security.bind_policy_error("0.0.0.0", None))  # nosec B104
        self.assertIn("PLUGIN_TOKEN", security.bind_policy_error("192.168.1.5", None))
        self.assertIn("shorter", security.bind_policy_error("192.168.1.5", "short"))
        self.assertIsNone(security.bind_policy_error("192.168.1.5", TOKEN))

    def test_plugin_server_refuses_non_loopback_without_token(self):
        with self.assertRaises(ValueError):
            PluginServer(object(), "0.0.0.0", 0, None)  # nosec B104 - must be refused

    def _run_main(self, **env):
        return subprocess.run([sys.executable, os.path.join(ROOT, "esp32_plugin.py")],
                              env=scrubbed_env(**env), capture_output=True, text=True,
                              timeout=30, check=False)

    def test_main_exits_non_zero_on_lan_bind_without_token(self):
        for bind in ("0.0.0.0", "192.168.125.70"):  # nosec B104 - must be refused
            r = self._run_main(PLUGIN_BIND=bind)
            self.assertEqual(r.returncode, 2, r)
            self.assertIn("PLUGIN_TOKEN", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_main_exits_non_zero_on_short_token(self):
        r = self._run_main(PLUGIN_BIND="0.0.0.0", PLUGIN_TOKEN="abc")  # nosec B104
        self.assertEqual(r.returncode, 2, r)
        self.assertIn("shorter", r.stderr)

    def test_main_listens_on_loopback_by_default(self):
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "esp32_plugin.py")],
                                env=scrubbed_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()
            proc.stderr.close()
        self.assertIn(" on 127.0.0.1:", line)
        self.assertIn("token off", line)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(token=TOKEN)
        self.addCleanup(self.h.close)

    def test_token_required_except_health(self):
        for headers in ({"X-Plugin-Token": ""}, {"X-Plugin-Token": "wrong"},
                        {"X-Plugin-Token": TOKEN + "x"}):
            for path in ("/devices", "/actions", "/jobs/j-000000000000"):
                code, r = self.h.call("GET", path, headers=headers)
                self.assertEqual((code, r["code"]), (401, "unauthorized"), (path, headers))
        self.assertEqual(self.h.call("GET", "/devices")[0], 200)

    def test_compare_is_constant_time(self):
        with mock.patch.object(ps.hmac, "compare_digest",
                               wraps=ps.hmac.compare_digest) as cd:
            self.assertEqual(self.h.call("GET", "/devices")[0], 200)
            self.assertTrue(cd.called)
        self.assertTrue(ps.token_matches(None, None))
        self.assertTrue(ps.token_matches(TOKEN, TOKEN))
        self.assertFalse(ps.token_matches(TOKEN, None))
        self.assertFalse(ps.token_matches(TOKEN, "ü"))

    def test_health_is_open_and_leaks_nothing(self):
        code, r = self.h.call("GET", "/health", headers={"X-Plugin-Token": ""})
        self.assertEqual(code, 200)
        self.assertEqual(r["contract"], ps.CONTRACT)
        self.assertNotIn("host", r)  # host name only for authenticated callers
        self.assertNotIn(TOKEN, json.dumps(r))
        code, r = self.h.call("GET", "/health")
        self.assertIn("host", r)
        self.assertNotIn(TOKEN, json.dumps(r))


def raw_request(port, method, path, headers=None, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders(body or None)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


class RequestLimitTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.port = self.h.server.port

    def test_oversize_json_is_413(self):
        code, r = raw_request(self.port, "POST", "/jobs",
                              {"Content-Length": str(ps.MAX_JSON_BYTES + 1)})
        self.assertEqual((code, r["code"]), (413, "too_large"))

    def test_oversize_upload_is_413(self):
        code, r = raw_request(self.port, "POST", "/uploads",
                              {"Content-Length": str(ps.MAX_UPLOAD_BYTES + 1)})
        self.assertEqual((code, r["code"]), (413, "too_large"))

    def test_bad_length_and_chunked(self):
        code, r = raw_request(self.port, "POST", "/jobs", {"Content-Length": "abc"})
        self.assertEqual(code, 400)
        code, r = raw_request(self.port, "POST", "/jobs", {"Content-Length": "-5"})
        self.assertEqual(code, 400)
        code, r = raw_request(self.port, "POST", "/jobs", {"Transfer-Encoding": "chunked"},
                              b"2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(code, 411)

    def test_bad_json_is_400(self):
        for body in (b"{not json", b"\xff\xfe", b"[" * 100_000):
            code, r = self.h.call("POST", "/jobs", raw=body)
            self.assertEqual((code, r["code"]), (400, "bad_request"), body[:10])
            self.assertNotIn("Traceback", r["error"])

    def test_timeout_ms_capped(self):
        code, r = self.h.call("POST", "/jobs", {"action": "clear",
                                                "timeout_ms": ps.MAX_TIMEOUT_MS + 1})
        self.assertEqual(code, 400)

    def test_bad_job_ids_are_404(self):
        for jid in ("x", "j-ZZZZ", "j-" + "0" * 13, "..%2f..%2fetc"):
            self.assertEqual(self.h.call("GET", f"/jobs/{jid}")[0], 404, jid)
            self.assertEqual(self.h.call("POST", f"/jobs/{jid}/cancel", {})[0], 404, jid)

    def test_no_traceback_to_clients(self):
        def boom(*_a, **_k):
            raise RuntimeError("secret-internal-detail")
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(self.h.plugin, "devices", boom):
            code, r = self.h.call("GET", "/devices")
        self.assertEqual(code, 500)
        self.assertNotIn("secret-internal-detail", json.dumps(r))
        action = self.h.plugin.actions()["clear"]
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(action, "run", boom):
            job = self.h.run("clear")
        self.assertEqual(job["state"], "error")
        blob = json.dumps(job)
        self.assertNotIn("secret-internal-detail", blob)
        self.assertNotIn("Traceback", blob)

    def test_bad_device_ref_is_refused(self):
        for ref in ("/etc/passwd", "/dev/sda", "/dev/serial/by-id/../../sda", "a" * 300,
                    "x\x00y"):
            code, r = self.h.call("POST", "/jobs", {"action": "clear", "device": ref})
            self.assertEqual((code, r["code"]), (404, "unknown_device"), ref)


class PathTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.build = make_result_build(self.root)
        self.h = Harness(firmware_root=self.root)
        self.addCleanup(self.h.close)

    def flash(self, firmware_dir):
        return self.h.call("POST", "/jobs", {"action": "flash",
                                             "params": {"firmware_dir": firmware_dir}})

    def test_traversal_and_outside_paths_are_400(self):
        outside = tempfile.mkdtemp(dir=os.path.dirname(self.root))
        os.symlink(outside, os.path.join(self.root, "link-out"))
        for value in ("../x", "result/../../etc", "/etc", outside, "link-out",
                      "result\x00", "upload:../../etc", "upload:XYZ"):
            code, r = self.flash(value)
            self.assertEqual((code, r["code"]), (400, "bad_request"), value)

    def test_inside_root_resolves(self):
        p = self.h.plugin.resolve_path("result")
        self.assertEqual(p, os.path.realpath(os.path.join(self.root, "result")))
        self.assertEqual(self.h.plugin.resolve_path(os.path.join(self.root, "result")), p)

    def test_extra_roots_from_env(self):
        extra = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"ESP32_FIRMWARE_ROOTS": extra}):
            self.assertEqual(self.h.plugin.resolve_path(extra), os.path.realpath(extra))


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            if isinstance(name, zipfile.ZipInfo):
                zf.writestr(name, data)
            else:
                zf.writestr(name, data)
    return buf.getvalue()


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def upload(self, data):
        return self.h.call("POST", "/uploads", raw=data,
                           headers={"Content-Type": "application/zip"})

    def test_only_expected_extensions_land(self):
        code, r = self.upload(zip_bytes([("build/flasher_args.json", "{}"),
                                         ("build/app.bin", b"\0" * 8),
                                         ("build/evil.sh", "rm -rf /"),
                                         ("build/.bashrc", "x")]))
        self.assertEqual(code, 201, r)
        self.assertTrue(os.path.isfile(os.path.join(r["path"], "build", "app.bin")))
        self.assertTrue(os.path.isfile(os.path.join(r["path"], "build", "flasher_args.json")))
        self.assertFalse(os.path.exists(os.path.join(r["path"], "build", "evil.sh")))
        self.assertFalse(os.path.exists(os.path.join(r["path"], "build", ".bashrc")))

    def test_bad_entries_are_400(self):
        link = zipfile.ZipInfo("build/link.bin")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in ([("../evil.bin", "x")], [("/abs.bin", "x")],
                        [("a/../../evil.bin", "x")], [(link, "/etc/passwd")]):
            code, r = self.upload(zip_bytes(entries))
            self.assertEqual(code, 400, entries)
        self.assertEqual(self.upload(b"not a zip")[0], 400)

    def test_zip_bomb_and_entry_count_are_413(self):
        with mock.patch.object(ps, "MAX_UNZIPPED_BYTES", 1000):
            code, r = self.upload(zip_bytes([("big.bin", b"\0" * 5000)]))
            self.assertEqual((code, r["code"]), (413, "too_large"))
        with mock.patch.object(ps, "MAX_UPLOAD_ENTRIES", 3):
            code, r = self.upload(zip_bytes([(f"{i}.bin", "x") for i in range(5)]))
            self.assertEqual((code, r["code"]), (413, "too_large"))

    def test_upload_root_must_be_ours_and_real(self):
        tmp = tempfile.mkdtemp()
        os.symlink(tmp, os.path.join(tmp, "link"))
        with self.assertRaises(RuntimeError):
            ps.Uploads(os.path.join(tmp, "link"))
        ps.Uploads(os.path.join(tmp, "real"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(tmp, "real")).st_mode), 0o700)


class ValidatorTests(unittest.TestCase):
    def test_serial_ports(self):
        for ok in ("/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller"
                   "_68a5-if00-port0", "/dev/ttyUSB0", "/dev/ttyACM12",
                   "/dev/cu.usbserial-0001", "/dev/cu.usbmodem1101"):
            self.assertTrue(security.is_serial_port(ok), ok)
        for bad in ("/dev/sda", "/etc/passwd", "/dev/serial/by-id/../../sda", "ttyUSB0",
                    "/dev/ttyUSB0; rm", "/dev/ttyUSBx", "-p", "/dev/ttyUSB0\x00"):
            self.assertFalse(security.is_serial_port(bad), bad)

    def test_macs_roles_job_ids(self):
        self.assertTrue(security.is_mac("7c:df:a1:a4:8f:00"))
        self.assertTrue(security.is_mac("7C:DF:A1:A4:8F:00"))
        for bad in ("7c:df:a1:a4:8f", "7c-df-a1-a4-8f-00", "zz:df:a1:a4:8f:00", 7):
            self.assertFalse(security.is_mac(bad), bad)
        for role in ("sensor", "hub", "server"):
            self.assertEqual(security.check_device_ref(role), role)
        self.assertEqual(security.check_device_ref(" /dev/fake0 ", ["/dev/fake0"]), "/dev/fake0")
        for bad in ("admin", "/dev/fake0", 5, "../x"):
            with self.assertRaises(ValueError):
                security.check_device_ref(bad)
        self.assertTrue(security.is_job_id("j-0123456789ab"))
        self.assertFalse(security.is_job_id("j-0123456789abc"))
        self.assertTrue(security.is_upload_id("0123456789ab"))
        self.assertFalse(security.is_upload_id("../0123456789"))

    def test_argv_values(self):
        self.assertEqual(security.check_argv_value("/dev/ttyUSB0"), "/dev/ttyUSB0")
        for bad in ("", "-rf", "--port", "a\x00b", None):
            with self.assertRaises(ValueError):
                security.check_argv_value(bad)


class FlasherArgsTests(unittest.TestCase):
    """flasher_args.json may come from an upload: nothing it says may smuggle
    options or files onto esptool's command line."""

    def setUp(self):
        self.build = make_result_build(tempfile.mkdtemp())
        with open(os.path.join(self.build, "flasher_args.json")) as f:
            self.args = json.load(f)

    def argv(self, args, port="/dev/ttyUSB0"):
        return esp32_plugin.build_flash_argv(port, self.build, args, 460800, False)

    def test_good_args_pass(self):
        argv = self.argv(self.args)
        self.assertIn("write_flash", argv)

    def test_bad_args_are_refused(self):
        def variant(**changes):
            a = json.loads(json.dumps(self.args))
            for k, v in changes.items():
                if k in ("chip", "before", "after"):
                    a.setdefault("extra_esptool_args", {})[k] = v
                else:
                    a[k] = v
            return a
        bad = [variant(write_flash_args=["--flash_mode", "dio;rm"]),
               variant(write_flash_args=["--port", "/dev/sda"]),
               variant(write_flash_args="--flash_mode dio"),
               variant(chip="esp32c3 --port x"), variant(before="--no-stub"),
               variant(after="run"),
               variant(flash_files={"0x0": "../../../../etc/passwd.bin"}),
               variant(flash_files={"0x0": "/etc/x.bin"}),
               variant(flash_files={"0x0": "bootloader/bootloader.elf"}),
               variant(flash_files={"0x0; rm": "temp_sensor.bin"})]
        for a in bad:
            with self.assertRaises(ActionError):
                self.argv(a)
        with self.assertRaises(ActionError):
            self.argv(self.args, port="--before")

    def test_image_symlink_out_of_dir_is_refused(self):
        outside = os.path.join(tempfile.mkdtemp(), "x.bin")
        with open(outside, "wb") as f:
            f.write(b"\0")
        os.symlink(outside, os.path.join(self.build, "out.bin"))
        a = dict(self.args, flash_files={"0x0": "out.bin"})
        with self.assertRaises(ActionError):
            self.argv(a)


class SharedTmpFileTests(unittest.TestCase):
    def test_role_cache_write_does_not_follow_planted_tmp_symlink(self):
        write = getattr(esp32_plugin, "cache_write", None) or esp32_plugin.lock_cache_write
        read = getattr(esp32_plugin, "cache_read", None) or esp32_plugin.lock_cache_read
        tmp = tempfile.mkdtemp()
        victim = os.path.join(tmp, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        cache = os.path.join(tmp, "roles.json")
        os.symlink(victim, cache + ".tmp")  # the old fixed temp name
        write(cache, "/dev/ttyUSB0", "sensor")
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")
        self.assertEqual(read(cache), {"/dev/ttyUSB0": "sensor"})

    def test_lock_file_symlink_is_not_followed(self):
        tmp = tempfile.mkdtemp()
        lock_dir = os.path.join(tmp, "locks")
        os.makedirs(lock_dir)
        victim = os.path.join(tmp, "victim")
        os.symlink(victim, os.path.join(lock_dir, esp32_plugin._lock_name("/dev/null")))
        self.assertIs(esp32_plugin.try_lock_port(lock_dir, "/dev/null"), False)
        self.assertFalse(os.path.exists(victim))

    def test_shared_defaults_unchanged(self):
        # The cross-process protocol with the sibling plugin: must match exactly.
        self.assertEqual(esp32_plugin.LOCK_DIR_DEFAULT, "/tmp/vt-esp32-locks")  # nosec B108
        self.assertEqual(esp32_plugin.ROLE_CACHE_DEFAULT,
                         "/tmp/vt-esp32-roles.json")  # nosec B108


if __name__ == "__main__":
    unittest.main()
