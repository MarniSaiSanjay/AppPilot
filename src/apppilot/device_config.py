"""Persist the device selected during ``/init`` and resolve it at runtime."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from . import device

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
    """Return the saved device serial."""
    return _read_saved_record()[0]


def load_saved_device_identity() -> Optional[str]:
    """Return the saved stable identity."""
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
    """Persist a non-empty device serial and stable identity."""
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
        pass


def _usable_serials(
    *,
    runner,
    timeout: float,
    list_devices: Optional[Callable[[], List["device.AdbDevice"]]],
    is_booted: Optional[Callable[[str], bool]],
) -> List[str]:
    """Return authorized and fully booted device serials."""
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
    """Resolve which device serial the runtime should target."""
    usable = _usable_serials(
        runner=runner, timeout=timeout,
        list_devices=list_devices, is_booted=is_booted,
    )

    if cli_device is not None and str(cli_device).strip():
        selected = str(cli_device).strip()
        if selected in usable:
            return selected
        output(
            f"Android device {selected!r} is not authorized and fully booted."
        )
        return None

    saved = saved_loader()
    if saved and saved in usable:
        expected_identity = saved_identity_loader()
        if expected_identity is None:
            # /init captured no identity (empty on Windows); trust the serial.
            return saved
        resolve_identity = identity_resolver or (
            lambda selected: _stable_device_identity(
                selected, runner=runner, timeout=timeout
            )
        )
        current_identity = resolve_identity(saved)
        if current_identity == expected_identity:
            return saved
        output(
            f"Saved Android device {saved!r} could not be verified as the "
            "device selected during /init. Run /init again or pass "
            "--device <serial>."
        )
        return None

    if len(usable) == 1:
        return usable[0]

    if len(usable) > 1:
        output(
            "Multiple usable Android devices detected: "
            + ", ".join(usable)
            + "\nPass --device <serial> to choose one."
        )
        return None

    output(
        "No authorized, fully booted Android device is available. "
        "Connect/start one and run /init or pass --device <serial>."
    )
    return None
