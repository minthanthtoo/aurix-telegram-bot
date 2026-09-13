#!/usr/bin/env python3
"""Launch bundled scrcpy deterministically over USB or Wi-Fi."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path


BUNDLE = Path("/Users/min/Downloads/scrcpy-macos-aarch64-v4.1")
ADB = BUNDLE / "adb"
SCRCPY = BUNDLE / "scrcpy"
USB_SERIAL_RE = re.compile(r"^[0-9A-Za-z]+$")
IP_RE = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/")


def run(command: list[str], *, capture: bool = True) -> str:
    result = subprocess.run(command, check=False, capture_output=capture, text=True)
    if result.returncode:
        message = result.stderr.strip() if capture else "command failed"
        raise RuntimeError(message)
    return result.stdout if capture else ""


def devices() -> list[str]:
    output = run([str(ADB), "devices"])
    return [line.split()[0] for line in output.splitlines()[1:] if line.endswith("\tdevice")]


def usb_devices() -> list[str]:
    return [serial for serial in devices() if USB_SERIAL_RE.fullmatch(serial)]


def wifi_endpoint(usb_serial: str, port: int) -> str:
    output = run(
        [str(ADB), "-s", usb_serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan0"]
    )
    match = IP_RE.search(output)
    if not match:
        raise RuntimeError("The phone has no Wi-Fi IPv4 address")
    endpoint = f"{match.group(1)}:{port}"
    run([str(ADB), "-s", usb_serial, "tcpip", str(port)])
    last_error = ""
    for attempt in range(8):
        if attempt:
            time.sleep(min(0.5 * attempt, 2.0))
        result = subprocess.run(
            [str(ADB), "connect", endpoint], check=False, capture_output=True, text=True
        )
        message = f"{result.stdout}\n{result.stderr}".strip()
        if result.returncode == 0 and "connected" in message.casefold():
            break
        last_error = message
    else:
        raise RuntimeError(last_error or f"could not connect to {endpoint}")
    for _ in range(10):
        state = subprocess.run(
            [str(ADB), "-s", endpoint, "get-state"], check=False, capture_output=True, text=True
        )
        if state.returncode == 0 and state.stdout.strip() == "device":
            return endpoint
        time.sleep(0.5)
    raise RuntimeError(f"Wi-Fi ADB endpoint did not become ready: {endpoint}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wifi", action="store_true", help="switch USB ADB to Wi-Fi")
    parser.add_argument("--serial", help="explicit USB serial or host:port")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--stay-awake", action="store_true")
    args, passthrough = parser.parse_known_args(argv)
    if not ADB.is_file() or not SCRCPY.is_file():
        parser.error(f"scrcpy bundle is incomplete: {BUNDLE}")
    serial = args.serial
    if not serial:
        usb = usb_devices()
        if usb:
            serial = usb[0]
        else:
            tcp = [item for item in devices() if ":" in item]
            if len(tcp) != 1:
                parser.error(f"expected one phone, found {len(tcp)} TCP devices and no USB device")
            serial = tcp[0]
    if args.wifi:
        serial = serial if ":" in serial else wifi_endpoint(serial, args.port)
    command = [str(SCRCPY), "--serial", serial]
    if args.no_audio:
        command.append("--no-audio")
    if args.stay_awake:
        command.append("--stay-awake")
    command.extend(passthrough)
    print(f"Launching scrcpy on {serial}", flush=True)
    return subprocess.call(command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"scrcpy-phone: {exc}", file=sys.stderr)
        raise SystemExit(2)
