#!/usr/bin/env python3
"""Stands in for esptool in tests: checks the argv shape, records it, and exits
with FAKE_ESPTOOL_RC (default 0).

Both ports are modelled: the CP2102N bridge (--baud, write_flash) and a native
USB-Serial-JTAG port, where esptool takes no --baud and the reset runs an
identify command (v5.x has --port, not --serial, and no probe/info command).
Every run ends with esptool's hard reset, which is also what resets the chip."""

import json
import os
import sys

argv = sys.argv[1:]
log = os.environ.get("FAKE_ESPTOOL_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\n")
print("esptool.py v4.8.1")
print("Connecting....")

COMMANDS = ("write_flash", "flash-id", "read-mac", "chip-id")
cmd = next((c for c in COMMANDS if c in argv), None)
assert cmd, argv
assert "--port" in argv, argv
port = argv[argv.index("--port") + 1]
if "ttyACM" in port or "usbmodem" in port:
    # A native CDC-ACM interface has no transfer baud rate.
    assert "--baud" not in argv, argv

if cmd == "write_flash":
    i = argv.index("write_flash")
    rest = argv[i + 1:]
    pairs = []
    j = 0
    while j < len(rest):
        if rest[j].startswith("--"):
            j += 1 if rest[j] == "--erase-all" else 2
            continue
        pairs.append(rest[j])
        j += 1
    for addr, path in zip(pairs[::2], pairs[1::2]):
        assert os.path.isfile(path), path
        print(f"Writing at {addr}... (100 %)")
        print(f"Wrote {os.path.getsize(path)} bytes at {addr}")
else:
    # Identify only: nothing is written. ESP32-C3 has no chip id, so esptool
    # falls back to the MAC; that is not a failure.
    print(f"Identify on {port}: nothing written")
    if cmd == "chip-id":
        print("No chip id (ESP32-C3): falling back to the MAC")

print("Hard resetting via RTS pin...")
sys.exit(int(os.environ.get("FAKE_ESPTOOL_RC", "0")))
