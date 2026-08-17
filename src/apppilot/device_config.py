"""Persist the device selected during ``/init`` and resolve it at runtime.

``/init`` discovers and validates an Android device (physical or emulator); this
module remembers the chosen serial so normal runtime can reuse it without the
operator re-passing ``--device`` every run. It deliberately reuses the same
lightweight, file-based persistence philosophy as the saved APK path / email
recipient (bounded read, best-effort write, gitignored, only a serial - never a
secret) rather than introducing a configuration framework.

Device *detection* still lives entirely in :mod:`apppilot.device`; this module
only adds a tiny store plus a resolution precedence on top of it:

    explicit --device  >  saved /init device  >  auto-detect  >  existing default

A saved serial is never blindly trusted - it is re-validated against the live
adb device list, and a stale value simply falls through to the next tier (it is
never overwritten by the fallback).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from . import device

# Selected device serial is remembered here (gitignored) - mirrors the saved APK
# path / email recipient stores. Contains ONLY the serial, nothing sensitive.
_DEVICE_STORE = Path(__file__).resolve().parents[2] / ".apppilot_device"

# Historical default preserved as the ultimate fallback so behavior is unchanged
# when nothing is saved and nothing usable is detected.
_DEFAULT_DEVICE = "emulator-5554"


def load_saved_device() -> Optional[str]:
    """Return the saved device serial, or ``None`` (bounded, never raises)."""
    try:
        # Bounded read so a corrupt/oversized store can never raise or blow up
        # memory; errors="replace" neutralizes non-UTF-8 content.
        with _DEVICE_STORE.open("rb") as handle:
            saved = handle.read(4096).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return saved or None


def save_device(serial: Optional[str]) -> None:
    """Best-effort persist a non-empty device serial. Never raises.

    Callers must only pass a serial they have already validated as authorized and
    booted, so an invalid/stale value can never overwrite a good saved one.
    """
    if not serial or not str(serial).strip():
        return
    try:
        _DEVICE_STORE.write_text(str(serial).strip() + "\n", encoding="utf-8")
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
    list_devices: Optional[Callable[[], List["device.AdbDevice"]]] = None,
    is_booted: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """Resolve which device serial the runtime should target.

    Precedence:
      1. An explicit ``--device`` (``cli_device``) is authoritative and returned
         verbatim - no adb probing, no re-validation, never overridden by the
         saved value. Preserves the exact behavior of existing explicit callers.
      2. Otherwise a saved ``/init`` serial is reused ONLY if it is still an
         authorized, booted device (re-validated live). A stale saved value is
         ignored and left untouched on disk.
      3. Otherwise, if exactly one usable device is present, auto-detect it.
      4. Multiple usable devices with no explicit/saved choice is ambiguous: a
         concise actionable message is emitted and ``None`` is returned (the
         caller exits cleanly and tells the user to pass ``--device <serial>``).
      5. Otherwise fall back to the historical default so behavior is unchanged
         when nothing is saved and nothing usable is detected.

    Returns the serial to use, or ``None`` when the caller should abort (ambiguous
    multi-device case only). Never raises.
    """
    # 1. Explicit --device wins in every mode, exactly as supplied.
    if cli_device is not None and str(cli_device).strip():
        return cli_device

    usable = _usable_serials(
        runner=runner, timeout=timeout,
        list_devices=list_devices, is_booted=is_booted,
    )

    # 2. A saved selection is honoured only while it is still usable.
    saved = saved_loader()
    if saved and saved in usable:
        return saved

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

    # 5. Nothing usable detected: preserve the historical default.
    return _DEFAULT_DEVICE
