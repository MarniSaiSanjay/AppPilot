"""Generic, reusable Android device capability layer.

Discovers adb devices and their states, discovers/inspects emulator AVDs, starts
an emulator (bounded), and determines boot readiness. This is framework-level:
it holds no ``/init`` orchestration, no prompting, no persistence, and no
use-case assumptions - only generic Android device behaviour that any caller
(``/init`` today, runtime tooling later) can reuse.

Every external command runs through :func:`apppilot.environment.run` - the safe,
exception-safe primitive (argument list, ``shell=False``, bounded timeout). The
runner, process launcher, and clock are dependency-injected so the whole layer
is testable without a real device or emulator.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import environment

# adb device states (from `adb devices`). "device" == authorized + online.
STATE_DEVICE = "device"
STATE_UNAUTHORIZED = "unauthorized"
STATE_OFFLINE = "offline"

# Bounded timeouts so a wedged device/emulator can never hang setup.
DEFAULT_ADB_TIMEOUT = 15.0
DEFAULT_BOOT_TIMEOUT = 180.0
DEFAULT_POLL_INTERVAL = 2.0

Runner = Callable[..., "subprocess.CompletedProcess"]
Popen = Callable[..., object]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# adb device discovery
# --------------------------------------------------------------------------- #
def _parse_adb_devices(output: str) -> List[AdbDevice]:
    """Parse ``adb devices`` output tolerantly across SDK versions.

    Skips the header, blank lines, adb-server/daemon notices, and any line
    without a ``<serial> <state>`` shape.
    """
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
    """Return whether Android has finished booting on ``serial``.

    Authoritative readiness signal: ``getprop sys.boot_completed == 1``. Merely
    appearing in ``adb devices`` is not proof of a booted system.
    """
    ok, output = environment.run(
        ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
        runner=runner, timeout=timeout,
    )
    return ok and output.strip() == "1"


def running_emulator_avd_name(
    serial: str,
    *, runner: Runner = subprocess.run, timeout: float = DEFAULT_ADB_TIMEOUT,
) -> Optional[str]:
    """Return the AVD name backing a running emulator ``serial``, or ``None``.

    Uses the emulator console (``adb -s <serial> emu avd name``), which answers
    only once the emulator is online.
    """
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


# --------------------------------------------------------------------------- #
# AVD discovery
# --------------------------------------------------------------------------- #
def list_avds(
    *, runner: Runner = subprocess.run, timeout: float = DEFAULT_ADB_TIMEOUT,
) -> List[str]:
    """Return installed AVD names via ``emulator -list-avds`` (empty on failure).

    Tolerant of the emulator's library/INFO noise, which it may interleave with
    the plain one-name-per-line listing.
    """
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


# --------------------------------------------------------------------------- #
# Emulator startup + boot readiness
# --------------------------------------------------------------------------- #
def start_emulator(avd_name: str, *, popen: Popen = subprocess.Popen) -> bool:
    """Launch ``avd_name`` detached, discarding its output. Never raises.

    Argument list only (``shell=False``); a new session detaches it from the
    setup process. The *decision* to start belongs to the caller - this only
    performs the bounded launch and reports whether spawning succeeded.
    """
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
    """Poll until ``serial`` is authorized and booted, or the timeout elapses.

    Bounded: returns ``True`` once online + ``sys.boot_completed == 1``, else
    ``False`` when the deadline passes. Never waits forever.
    """
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
    """Wait for ``avd_name`` to appear, come online and boot; return its serial.

    Used after starting an emulator whose serial is not known in advance: polls
    ``adb devices`` for an authorized emulator whose console reports ``avd_name``
    and whose system has booted. Returns the serial, or ``None`` on timeout.
    """
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
