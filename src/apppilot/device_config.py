"""Persist the device selected during ``/init`` and resolve it at runtime.

``/init`` discovers and validates an Android device (physical or emulator); this
module remembers the chosen serial so normal runtime can reuse it without the
operator re-passing ``--device`` every run. It deliberately reuses the same
lightweight, file-based persistence philosophy as the saved APK path / email
recipient (bounded read, best-effort write, gitignored, no secrets) rather than
introducing a configuration framework.

Device *detection* still lives entirely in :mod:`apppilot.device`; this module
only adds a tiny store plus a resolution precedence on top of it:

    explicit --device  >  saved /init device  >  auto-detect  >  clean failure

A saved serial is never blindly trusted - it is re-validated against the live
adb device list, and a stale value simply falls through to the next tier (it is
never overwritten by the fallback).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from . import device

# Selected device metadata is remembered here (gitignored) - mirrors the saved
# APK path / email recipient stores. Contains no credentials or secrets.
_DEVICE_STORE = Path(__file__).resolve().parents[2] / ".apppilot_device"


def _read_saved_record() -> tuple[Optional[str], Optional[str]]:
    try:
        with _DEVICE_STORE.open("rb") as handle:
            raw = handle.read(4096).decode("utf-8", "replace").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    if not raw.startswith("{"):
        return raw, None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(decoded, dict):
        return None, None
    serial = decoded.get("serial")
    identity = decoded.get("identity")
    if not isinstance(serial, str) or not serial.strip():
        return None, None
    if identity is not None and (
        not isinstance(identity, str) or not identity.strip()
    ):
        return None, None
    return serial.strip(), identity.strip() if identity else None


def load_saved_device() -> Optional[str]:
    """Return the saved device serial, or ``None`` (bounded, never raises)."""
    return _read_saved_record()[0]


def load_saved_device_identity() -> Optional[str]:
    """Return the saved stable identity, or ``None`` (bounded, never raises)."""
    return _read_saved_record()[1]


def _stable_device_identity(
    serial: str,
    *,
    runner=subprocess.run,
    timeout: float = device.DEFAULT_ADB_TIMEOUT,
) -> Optional[str]:
    normalized = serial.strip()
    if not normalized:
        return None
    if normalized.startswith("emulator-"):
        avd_name = device.running_emulator_avd_name(
            normalized, runner=runner, timeout=timeout
        )
        return f"avd:{avd_name}" if avd_name else None
    return f"serial:{normalized}"


def save_device(
    serial: Optional[str],
    *,
    runner=subprocess.run,
    timeout: float = device.DEFAULT_ADB_TIMEOUT,
    identity_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> None:
    """Best-effort persist a non-empty device serial and stable identity.

    Callers must only pass a serial they have already validated as authorized and
    booted, so an invalid/stale value can never overwrite a good saved one.
    """
    if not serial or not str(serial).strip():
        return
    normalized = str(serial).strip()
    resolve_identity = identity_resolver or (
        lambda selected: _stable_device_identity(
            selected, runner=runner, timeout=timeout
        )
    )
    identity = resolve_identity(normalized)
    try:
        _DEVICE_STORE.write_text(
            json.dumps(
                {"serial": normalized, "identity": identity},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # Persisting the selection is best-effort; never block the run.


def _usable_serials(
    *,
    runner,
    timeout: float,
    list_devices: Optional[Callable[[], List["device.AdbDevice"]]],
    is_booted: Optional[Callable[[str], bool]],
) -> List[str]:
    """Serials of devices that are authorized AND fully booted (usable now)."""
    list_devices = list_devices or (
        lambda: device.list_adb_devices(runner=runner, timeout=timeout)
    )
    is_booted = is_booted or (
        lambda serial: device.is_boot_completed(
            serial, runner=runner, timeout=timeout,
        )
    )
    return [
        d.serial for d in list_devices()
        if d.is_authorized and is_booted(d.serial)
    ]


def resolve_device(
    cli_device: Optional[str],
    *,
    runner=subprocess.run,
    timeout: float = device.DEFAULT_ADB_TIMEOUT,
    output: Callable[[str], None] = print,
    saved_loader: Callable[[], Optional[str]] = load_saved_device,
    saved_identity_loader: Callable[
        [], Optional[str]
    ] = load_saved_device_identity,
    list_devices: Optional[Callable[[], List["device.AdbDevice"]]] = None,
    is_booted: Optional[Callable[[str], bool]] = None,
    identity_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Resolve which device serial the runtime should target.

    Precedence:
      1. An explicit ``--device`` (``cli_device``) is authoritative only when it
         is currently authorized and fully booted.
      2. Otherwise a saved ``/init`` serial is reused ONLY if it is still an
         authorized, booted device and its stable identity still matches.
         A stale saved value is ignored and left untouched on disk.
      3. Otherwise, if exactly one usable device is present, auto-detect it.
      4. Multiple usable devices with no explicit/saved choice is ambiguous: a
         concise actionable message is emitted and ``None`` is returned (the
         caller exits cleanly and tells the user to pass ``--device <serial>``).
      5. Otherwise fail cleanly with an actionable no-device message.

    Returns the serial to use, or ``None`` when the caller should abort. Never
    raises.
    """
    usable = _usable_serials(
        runner=runner, timeout=timeout,
        list_devices=list_devices, is_booted=is_booted,
    )

    # 1. An explicit device is authoritative only when it is usable now.
    if cli_device is not None and str(cli_device).strip():
        selected = str(cli_device).strip()
        if selected in usable:
            return selected
        output(
            f"Android device {selected!r} is not authorized and fully booted."
        )
        return None

    # 2. A saved selection is honoured only while it is still usable.
    saved = saved_loader()
    if saved and saved in usable:
        expected_identity = saved_identity_loader()
        resolve_identity = identity_resolver or (
            lambda selected: _stable_device_identity(
                selected, runner=runner, timeout=timeout
            )
        )
        current_identity = resolve_identity(saved)
        if (
            expected_identity is not None
            and current_identity == expected_identity
        ):
            return saved
        output(
            f"Saved Android device {saved!r} could not be verified as the "
            "device selected during /init. Run /init again or pass "
            "--device <serial>."
        )
        return None

    # 3. Exactly one usable device -> use it deterministically.
    if len(usable) == 1:
        return usable[0]

    # 4. Ambiguous: never guess between multiple devices at runtime.
    if len(usable) > 1:
        output(
            "Multiple usable Android devices detected: "
            + ", ".join(usable)
            + "\nPass --device <serial> to choose one."
        )
        return None

    # 5. Nothing usable detected: fail now rather than guessing a serial.
    output(
        "No authorized, fully booted Android device is available. "
        "Connect/start one and run /init or pass --device <serial>."
    )
    return None
