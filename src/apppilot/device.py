"""Generic Android device capability layer."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import environment

STATE_DEVICE = "device"
STATE_UNAUTHORIZED = "unauthorized"
STATE_OFFLINE = "offline"

DEFAULT_ADB_TIMEOUT = 15.0
DEFAULT_BOOT_TIMEOUT = 180.0
DEFAULT_POLL_INTERVAL = 2.0

Runner = Callable[..., "subprocess.CompletedProcess"]
Popen = Callable[..., object]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class AdbDevice:
    """A single entry from ``adb devices`` - a serial and its reported state."""

    serial: str
    state: str

    @property
    def is_emulator(self) -> bool:
        return self.serial.startswith("emulator-")

    @property
    def is_authorized(self) -> bool:
        return self.state == STATE_DEVICE


def _parse_adb_devices(output: str) -> List[AdbDevice]:
    """Parse ``adb devices`` output."""
    devices: List[AdbDevice] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        lower = line.lower()
        if lower.startswith("list of devices"):
            continue
        if lower.startswith("adb server") or "daemon" in lower:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        devices.append(AdbDevice(serial=parts[0], state=parts[1]))
    return devices


def list_adb_devices(
    *, runner: Runner = subprocess.run, timeout: float = DEFAULT_ADB_TIMEOUT,
) -> List[AdbDevice]:
    """Return all devices adb currently reports (empty on any failure)."""
    ok, output = environment.run(["adb", "devices"], runner=runner, timeout=timeout)
    if not ok:
        return []
    return _parse_adb_devices(output)


def is_boot_completed(
    serial: str,
    *, runner: Runner = subprocess.run, timeout: float = DEFAULT_ADB_TIMEOUT,
) -> bool:
    """Return whether Android has finished booting on ``serial``."""
    ok, output = environment.run(
        ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
        runner=runner, timeout=timeout,
    )
    return ok and output.strip() == "1"


def running_emulator_avd_name(
    serial: str,
    *, runner: Runner = subprocess.run, timeout: float = DEFAULT_ADB_TIMEOUT,
) -> Optional[str]:
    """Return the AVD name backing a running emulator."""
    ok, output = environment.run(
        ["adb", "-s", serial, "emu", "avd", "name"], runner=runner, timeout=timeout,
    )
    if not ok:
        return None
    for raw in output.splitlines():
        line = raw.strip()
        if line and line.upper() != "OK":
            return line
    return None


def find_running_avd(
    avd_name: str,
    *, runner: Runner = subprocess.run, timeout: float = DEFAULT_ADB_TIMEOUT,
) -> Optional[AdbDevice]:
    """Return the authorized emulator currently running ``avd_name``, or None."""
    for device in list_adb_devices(runner=runner, timeout=timeout):
        if not device.is_emulator or not device.is_authorized:
            continue
        if running_emulator_avd_name(device.serial, runner=runner, timeout=timeout) \
                == avd_name:
            return device
    return None


def list_avds(
    *, runner: Runner = subprocess.run, timeout: float = DEFAULT_ADB_TIMEOUT,
) -> List[str]:
    """Return installed AVD names."""
    ok, output = environment.run(
        ["emulator", "-list-avds"], runner=runner, timeout=timeout,
    )
    if not ok:
        return []
    avds: List[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or "|" in line:
            continue
        if line.lower().startswith(("info", "warning", "error", "panic", "library")):
            continue
        avds.append(line)
    return avds


def start_emulator(avd_name: str, *, popen: Popen = subprocess.Popen) -> bool:
    """Launch ``avd_name`` detached."""
    try:
        popen(
            ["emulator", "-avd", avd_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, ValueError):
        return False


def wait_for_boot(
    serial: str,
    *,
    runner: Runner = subprocess.run,
    timeout: float = DEFAULT_BOOT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> bool:
    """Poll until ``serial`` is authorized and booted."""
    deadline = clock() + timeout
    while True:
        by_serial = {d.serial: d for d in list_adb_devices(runner=runner)}
        device = by_serial.get(serial)
        if device is not None and device.is_authorized \
                and is_boot_completed(serial, runner=runner):
            return True
        if clock() >= deadline:
            return False
        sleep(poll_interval)


def wait_for_emulator(
    avd_name: str,
    *,
    runner: Runner = subprocess.run,
    timeout: float = DEFAULT_BOOT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> Optional[str]:
    """Wait for ``avd_name`` to boot and return its serial."""
    deadline = clock() + timeout
    while True:
        for device in list_adb_devices(runner=runner):
            if not device.is_emulator or not device.is_authorized:
                continue
            if running_emulator_avd_name(device.serial, runner=runner) != avd_name:
                continue
            if is_boot_completed(device.serial, runner=runner):
                return device.serial
        if clock() >= deadline:
            return None
        sleep(poll_interval)
