"""Emulator auto-start node.

A self-contained capability with ONE responsibility: when no device is
connected, start one of the machine's existing AVDs and wait until it is booted
and usable, so the suite can proceed instead of failing.

This is deliberately separate from ``device_check`` because it is
*side-effecting* (it launches a long-running emulator process), whereas the
check is a read-only query. It only ever helps when an emulator ALREADY exists:

  * no Android SDK / ``emulator`` binary -> cannot help, returns not ok.
  * SDK present but no AVDs defined -> cannot help, returns not ok.
  * an AVD exists (running or not) -> start one and wait for boot.

Every external seam (AVD listing, process launch, adb polling, sleeping) is
injectable, so the logic is fully testable without a real emulator or waiting.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

# Injectable seams (defaults use the real SDK / adb / subprocess / time).
CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]
Launcher = Callable[[Sequence[str]], None]
Sleeper = Callable[[float], None]

# The first emulator can take a while to boot; poll adb until it is ready.
_BOOT_TIMEOUT_SECONDS = 180.0
_BOOT_POLL_INTERVAL_SECONDS = 3.0
_ADB_QUERY_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class EmulatorStartResult:
    """Outcome of an auto-start attempt.

    ``serial`` is the freshly booted device's adb serial (usable as ``--device``)
    on success, else ``None``. ``message`` explains the outcome either way.
    """

    ok: bool
    serial: Optional[str]
    message: str


def _sdk_root() -> Optional[str]:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(var, "").strip()
        if value and Path(value).is_dir():
            return value
    default = Path.home() / "Library" / "Android" / "sdk"
    return str(default) if default.is_dir() else None


def emulator_binary() -> Optional[str]:
    """Path to the SDK ``emulator`` binary, or None if it cannot be located."""
    root = _sdk_root()
    if root:
        candidate = Path(root) / "emulator" / "emulator"
        if candidate.exists():
            return str(candidate)
    return None


def _default_runner(args: Sequence[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        timeout=_ADB_QUERY_TIMEOUT_SECONDS,
    )


def _default_launcher(args: Sequence[str]) -> None:
    # Detach fully: the emulator must outlive this call and not tie its stdio to
    # the suite. start_new_session prevents it from dying with the parent's group.
    subprocess.Popen(
        list(args),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def list_avds(
    binary: str, runner: CommandRunner = _default_runner
) -> "list[str]":
    """Return the machine's AVD names via ``emulator -list-avds`` (empty on error)."""
    try:
        result = runner([binary, "-list-avds"])
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]


def _ready_serials(runner: CommandRunner) -> "set[str]":
    """Serials currently in adb 'device' (booted/ready) state."""
    try:
        result = runner(["adb", "devices"])
    except (subprocess.SubprocessError, OSError):
        return set()
    ready = set()
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            ready.add(parts[0])
    return ready


def _is_booted(serial: str, runner: CommandRunner) -> bool:
    try:
        result = runner(
            ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"]
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0 and (result.stdout or "").strip() == "1"


def ensure_emulator_running(
    *,
    avd: Optional[str] = None,
    runner: CommandRunner = _default_runner,
    launcher: Launcher = _default_launcher,
    sleeper: Sleeper = time.sleep,
    boot_timeout: float = _BOOT_TIMEOUT_SECONDS,
    poll_interval: float = _BOOT_POLL_INTERVAL_SECONDS,
) -> EmulatorStartResult:
    """Start an existing AVD and wait until it is booted and adb-ready.

    Picks ``avd`` when given (must exist), else the first available AVD. Returns
    the new device's serial on success. Never raises - all failures become an
    ``ok=False`` result with an actionable message.
    """
    binary = emulator_binary()
    if binary is None:
        return EmulatorStartResult(
            False,
            None,
            "the Android SDK 'emulator' tool was not found (set ANDROID_HOME), "
            "so no emulator could be started.",
        )

    avds = list_avds(binary, runner)
    if not avds:
        return EmulatorStartResult(
            False,
            None,
            "no emulators (AVDs) are defined on this machine, so none could be "
            "started.",
        )

    if avd is not None and avd not in avds:
        listed = ", ".join(avds)
        return EmulatorStartResult(
            False,
            None,
            f"requested AVD '{avd}' does not exist. Available AVDs: {listed}.",
        )

    chosen = avd or sorted(avds)[0]
    if avd is None and len(avds) > 1:
        # Several AVDs and no explicit choice: pick deterministically (first,
        # sorted) so a run is reproducible, and tell the operator how to override.
        listed = ", ".join(sorted(avds))
        print(
            f"[EMULATOR] multiple AVDs found ({listed}); starting '{chosen}'. "
            "Pass --avd <name> to choose a specific one."
        )
    # Snapshot the already-ready serials so we can identify the NEW one that this
    # launch brings online (robust when other devices are already attached).
    before = _ready_serials(runner)

    print(f"[EMULATOR] no device connected; starting AVD '{chosen}'...")
    try:
        launcher([binary, "-avd", chosen])
    except (subprocess.SubprocessError, OSError) as error:
        return EmulatorStartResult(
            False, None, f"failed to launch emulator '{chosen}': {error}"
        )

    deadline = time.monotonic() + boot_timeout
    while time.monotonic() < deadline:
        sleeper(poll_interval)
        new_ready = _ready_serials(runner) - before
        for serial in sorted(new_ready):
            if _is_booted(serial, runner):
                print(f"[EMULATOR] '{chosen}' booted as {serial}")
                return EmulatorStartResult(
                    True, serial, f"started AVD '{chosen}' as {serial}."
                )

    return EmulatorStartResult(
        False,
        None,
        f"emulator '{chosen}' did not become ready within "
        f"{int(boot_timeout)}s - check it manually with 'adb devices'.",
    )
