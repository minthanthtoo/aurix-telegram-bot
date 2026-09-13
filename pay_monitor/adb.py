"""Small subprocess-only ADB adapter with no shell interpolation."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


DEFAULT_ADB = Path("/Users/min/Downloads/scrcpy-macos-aarch64-v4.1/adb")


class AdbError(RuntimeError):
    pass


class ForegroundInterrupted(AdbError):
    pass


class AdbClient:
    def __init__(self, adb_path: str | Path | None = None, serial: str | None = None):
        configured = adb_path or os.environ.get("ADB_PATH") or DEFAULT_ADB
        self.adb_path = Path(configured)
        self.serial = serial or os.environ.get("ANDROID_SERIAL")

    def _base(self) -> list[str]:
        command = [str(self.adb_path)]
        if self.serial:
            command.extend(("-s", self.serial))
        return command

    def run(self, *args: str, timeout: int = 20, binary: bool = False) -> str | bytes:
        if not self.adb_path.is_file():
            raise AdbError(f"ADB binary not found: {self.adb_path}")
        try:
            result = subprocess.run(
                [*self._base(), *args],
                check=False,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbError(f"ADB command failed: {args[0] if args else 'unknown'}") from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise AdbError(message or "ADB command returned an error")
        return result.stdout if binary else result.stdout.decode("utf-8", "replace")

    def shell(self, *args: str, timeout: int = 20) -> str:
        return str(self.run("shell", *args, timeout=timeout))

    def require_one_device(self) -> str:
        if self.serial:
            state = str(self.run("get-state")).strip()
            if state != "device":
                raise AdbError(f"Selected Android device is {state or 'unavailable'}")
            return self.serial
        lines = str(self.run("devices", "-l")).splitlines()[1:]
        devices = [line for line in lines if " device " in f" {line} "]
        if len(devices) != 1:
            raise AdbError(f"Expected one authorized device, found {len(devices)}")
        serial = devices[0].split()[0]
        self.serial = serial
        return serial

    def start_package(self, package: str, *, force_stop: bool = True) -> None:
        output = self.shell(
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-c",
            "android.intent.category.LAUNCHER",
            package,
        )
        component = next((line.strip() for line in output.splitlines() if "/" in line), "")
        if not component.startswith(f"{package}/"):
            raise AdbError(f"Could not resolve launcher activity for {package}")
        arguments = ["am", "start"]
        if force_stop:
            arguments.append("-S")
        arguments.extend(("-W", "-n", component))
        self.shell(*arguments, timeout=30)

    def current_package(self) -> str | None:
        output = self.shell("dumpsys", "activity", "activities")
        for marker in ("topResumedActivity=", "mResumedActivity:"):
            line = next((item for item in output.splitlines() if marker in item), "")
            match = re.search(r"\bu\d+\s+([^\s/]+)/", line)
            if match:
                return match.group(1)
        return None

    def ui_xml(self) -> str:
        remote = "/sdcard/aurix-pay-monitor-window.xml"
        self.shell("uiautomator", "dump", remote)
        try:
            return str(self.run("exec-out", "cat", remote))
        finally:
            try:
                self.shell("rm", "-f", remote)
            except AdbError:
                pass

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def screencap_png(self) -> bytes:
        return bytes(self.run("exec-out", "screencap", "-p", binary=True, timeout=10))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 350) -> None:
        self.shell(
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        )

    def keyevent(self, key: str) -> None:
        self.shell("input", "keyevent", key)

    def window_size(self) -> tuple[int, int]:
        output = self.shell("wm", "size")
        size = output.rsplit(":", 1)[-1].strip().splitlines()[0]
        width, height = size.split("x", 1)
        return int(width), int(height)

    def version_name(self, package: str) -> str:
        output = self.shell("dumpsys", "package", package)
        for line in output.splitlines():
            if line.strip().startswith("versionName="):
                return line.split("=", 1)[1].strip()
        return "unknown"

    def notification_dump(self) -> str:
        return self.shell("dumpsys", "notification", "--noredact", timeout=30)

    def media_rows(self) -> str:
        return self.shell(
            "content",
            "query",
            "--uri",
            "content://media/external/images/media",
            "--projection",
            "_id:relative_path:_display_name:date_added:owner_package_name",
            timeout=30,
        )
