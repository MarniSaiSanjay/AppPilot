"""Device preflight node.

A small, self-contained capability with ONE responsibility: confirm a usable
Android target (device or emulator) is attached before the suite starts.

The rest of the flow assumes a device is connected; without one every adb/Maestro
call fails mid-run with a cryptic error. This node checks up front and returns a
structured, deterministic verdict so the orchestrator can fail fast with an
actionable message. It performs no retries and has no side effects (a read-only
``adb devices`` query), and the query runner is injectable for testing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

# runner(args) -> CompletedProcess; injectable so tests need no real adb/device.
DeviceQueryRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]

_ADB_QUERY_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class DeviceCheckResult:
    """Outcome of the preflight check.

    ``ok`` is the gate the orchestrator branches on; ``message`` is a
    human-readable, actionable explanation for the log either way.

    ``device_id`` is the serial to ACTUALLY use: the requested one when it is
    ready, or an auto-selected fallback when the request was absent but a single
    other device is ready (``None`` when not ok). ``any_device_present`` tells the
    orchestrator whether adb saw *any* device at all - when False, the caller may
    choose to start an emulator rather than give up.
    """

    ok: bool
    message: str
    device_id: Optional[str] = None
    any_device_present: bool = False


def _default_runner(args: Sequence[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        timeout=_ADB_QUERY_TIMEOUT_SECONDS,
    )


def _parse_device_states(stdout: str) -> "dict[str, str]":
    """Parse ``adb devices`` output into ``{serial: state}``.

    Skips the "List of devices attached" header; each remaining non-empty line
    is ``<serial>\\t<state>`` (state e.g. ``device``/``unauthorized``/``offline``).
    """
    states: dict[str, str] = {}
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def _explain_unready(
    device_id: str, states: Mapping[str, str], ready: Sequence[str]
) -> str:
    """Actionable message for a target that is present-but-unusable or when
    several devices are ready but none is the requested one (ambiguous)."""
    state = states.get(device_id)
    if state == "unauthorized":
        return (
            f"device {device_id} is 'unauthorized' - accept the USB-debugging / "
            "RSA prompt on the device."
        )
    if state == "offline":
        return (
            f"device {device_id} is 'offline' - reconnect it or restart the "
            "emulator (adb kill-server && adb start-server)."
        )
    if ready:
        # Several ready devices, none is the requested serial: don't guess which
        # to run destructive installs on - let the operator choose.
        listed = ", ".join(sorted(ready))
        return (
            f"device {device_id} not found. Ready devices: {listed} - "
            "re-run with --device <serial>."
        )
    return (
        "no Android device or emulator is connected - start an emulator or plug "
        "in a device (verify with 'adb devices')."
    )


def check_device_ready(
    device_id: str, runner: DeviceQueryRunner = _default_runner
) -> DeviceCheckResult:
    """Return whether ``device_id`` is attached and ready for the suite.

    Ready == present in ``adb devices`` with state ``device``. If the requested
    device is absent but exactly ONE other device is ready, that one is selected
    automatically (``ok=True`` with a note). Any other outcome (adb missing/error,
    unauthorized/offline, several-but-wrong, or nothing connected) yields
    ``ok=False`` with a message that names the concrete next step.
    """
    try:
        result = runner(["adb", "devices"])
    except FileNotFoundError:
        return DeviceCheckResult(
            False,
            "adb not found on PATH - install the Android platform-tools "
            "(https://developer.android.com/tools/releases/platform-tools).",
        )
    except (subprocess.SubprocessError, OSError) as error:
        return DeviceCheckResult(False, f"could not run adb: {error}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return DeviceCheckResult(
            False, f"adb devices failed: {detail or 'unknown error'}"
        )

    states = _parse_device_states(result.stdout)
    any_present = bool(states)
    if states.get(device_id) == "device":
        return DeviceCheckResult(
            True,
            f"device {device_id} is connected and ready.",
            device_id=device_id,
            any_device_present=any_present,
        )

    ready = [serial for serial, state in states.items() if state == "device"]
    # Requested device absent but exactly one other is ready: use it.
    if states.get(device_id) is None and len(ready) == 1:
        chosen = ready[0]
        return DeviceCheckResult(
            True,
            f"requested device {device_id} not found; using the only connected "
            f"device {chosen} instead.",
            device_id=chosen,
            any_device_present=any_present,
        )

    return DeviceCheckResult(
        False,
        _explain_unready(device_id, states, ready),
        any_device_present=any_present,
    )
