"""Preflight check - the single "are we ready to run?" gate.

Runs every hard prerequisite the suite needs BEFORE it touches the app, so a
missing tool, device, setting, or path fails fast with one clear message instead
of crashing mid-run. It composes the focused sub-nodes rather than
re-implementing them:

  1. Python interpreter new enough  (python_check)
  2. Maestro CLI present            (maestro_check)
  3. Evaluation model configured    (model_check)
  3b. Sign-in credentials present   (credentials_check; advisory warning only)
  4. Test-cases workbook resolved   (path_setup, prompting/persisting once)
  5. APK source chosen              (apk_source: build / existing / playstore,
                                     confirmed every interactive run)
  6. Source-specific prerequisites:
       * build     - Office Mobile enlistment (path_setup) + build toolchain
                     (build_tools_check: JDK 17 + omrdroid)
       * existing  - the existing APK path (path_setup, prompting once)
       * playstore - nothing extra (AppPilot installs nothing)
  7. A usable device resolved       (device_check, auto-starting an emulator
                                     via emulator_autostart when nothing is
                                     connected)

Fast, non-interactive checks run first; the interactive setup (paths + APK
source) runs before the slow device/emulator step. The enlistment + build
toolchain are only required when the operator chooses to build locally, so
AppPilot also works with a prebuilt APK or the Play Store app. On success it
returns the resolved device serial, the resolved paths, and the chosen APK
source (plus the APK path when one applies). Never raises; every failure yields
``ok=False`` with an actionable, operator-facing message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from . import apk_source
from . import build_tools_check
from . import credentials_check
from . import device_check
from . import emulator_autostart
from . import maestro_check
from . import model_check
from . import path_setup
from . import python_check
from .config_store import ConfigStore

# Config keys under which resolved paths are remembered between runs.
TEST_CASES_KEY = "test_cases_path"
ENLISTMENT_KEY = "om_enlistment_root"
EXISTING_APK_KEY = "existing_apk_path"

# Injectable check seams (defaults call the real nodes). Named "*_checker" /
# "*_starter" / "*_selector" so they never shadow the node modules they default
# to.
PythonChecker = Callable[[], "python_check.CheckResult"]
MaestroChecker = Callable[[], "maestro_check.ToolCheckResult"]
BuildToolsChecker = Callable[[], "build_tools_check.CheckResult"]
ModelChecker = Callable[[Mapping[str, str]], "model_check.ModelCheckResult"]
CredentialsChecker = Callable[
    [Mapping[str, str]], "credentials_check.CredentialsCheckResult"
]
ApkSourceSelector = Callable[..., "apk_source.ApkSourceResult"]
DeviceChecker = Callable[[str], "device_check.DeviceCheckResult"]
EmulatorStarter = Callable[..., "emulator_autostart.EmulatorStartResult"]


@dataclass(frozen=True)
class PreflightResult:
    """Aggregate readiness verdict.

    On success (``ok``) it carries everything ``main`` needs to run: the adb
    ``device_id`` to target, the ``test_cases_path`` workbook to load, and how to
    put the app on the device via ``apk_source`` (``"build"`` /  ``"existing"`` /
    ``"playstore"``). For ``"build"`` it also carries the ``enlistment_root`` the
    APK build should use; for ``"existing"`` it carries the ready-to-install
    ``apk_path``; for ``"playstore"`` neither applies (AppPilot installs
    nothing). ``message`` explains the first failure, or a success summary.
    """

    ok: bool
    device_id: Optional[str] = None
    test_cases_path: Optional[str] = None
    apk_source: Optional[str] = None
    enlistment_root: Optional[str] = None
    apk_path: Optional[str] = None
    message: str = ""


def _log(message: str) -> None:
    print(f"[PREFLIGHT] {message}")


def _warn(message: str) -> None:
    print(f"[PREFLIGHT] WARNING: {message}")


def _resolve_device(
    device: str,
    avd: Optional[str],
    autostart_emulator: bool,
    device_checker: DeviceChecker,
    emulator_starter: EmulatorStarter,
) -> device_check.DeviceCheckResult:
    """Resolve a ready device: check what's connected, else auto-start an AVD.

    Returns a ``DeviceCheckResult`` whose ``ok``/``message``/``device_id`` the
    caller reports. The emulator node is only engaged when nothing at all is
    connected and auto-start is enabled.
    """
    status = device_checker(device)
    if status.ok:
        return status

    # A device is present but unusable (unauthorized/offline), or several ready
    # devices exist but none is the requested one: the operator must act -
    # starting another emulator would not resolve it.
    if status.any_device_present or not autostart_emulator:
        return status

    # Nothing usable is connected: let the emulator node try to bring one up.
    # (It logs the concrete AVD it starts, so no extra log line is needed here.)
    started = emulator_starter(avd=avd)
    if not started.ok:
        return device_check.DeviceCheckResult(
            False,
            "no usable Android device. "
            f"{started.message} Connect a real device with USB debugging "
            "enabled, or create/start an emulator, then retry.",
        )
    # Confirm the freshly booted emulator is genuinely ready.
    return device_checker(started.serial)


def run_preflight(
    *,
    device: str,
    avd: Optional[str] = None,
    autostart_emulator: bool = True,
    env: Mapping[str, str],
    test_cases_default: Optional[str] = None,
    test_cases_override: Optional[str] = None,
    enlistment_default: Optional[str] = None,
    enlistment_override: Optional[str] = None,
    apk_source_default: str = apk_source.BUILD,
    interactive: bool = False,
    prompter: path_setup.Prompter = input,
    store: Optional[ConfigStore] = None,
    python_checker: PythonChecker = python_check.check_python_version,
    maestro_checker: MaestroChecker = maestro_check.check_maestro_ready,
    build_tools_checker: BuildToolsChecker = build_tools_check.check_build_tools,
    model_checker: ModelChecker = model_check.check_model_configured,
    credentials_checker: CredentialsChecker = (
        credentials_check.check_credentials_configured
    ),
    apk_source_selector: ApkSourceSelector = apk_source.select_apk_source,
    device_checker: DeviceChecker = device_check.check_device_ready,
    emulator_starter: EmulatorStarter = emulator_autostart.ensure_emulator_running,
) -> PreflightResult:
    """Verify every prerequisite and resolve paths. See the module docstring."""
    store = ConfigStore() if store is None else store

    # 1) Python interpreter.
    python_status = python_checker()
    if not python_status.ok:
        return PreflightResult(False, message=python_status.message)
    _log(python_status.message)

    # 2) Maestro CLI.
    maestro_status = maestro_checker()
    if not maestro_status.ok:
        return PreflightResult(False, message=maestro_status.message)
    _log(maestro_status.message)

    # 3) Evaluation model configuration.
    model_status = model_checker(env)
    if not model_status.ok:
        return PreflightResult(False, message=model_status.message)
    _log(model_status.message)

    # Sign-in credentials (advisory, never fatal): login is a no-op when the
    # app is already signed in, so we warn rather than block when unset.
    credentials_status = credentials_checker(env)
    if credentials_status.configured:
        _log(credentials_status.message)
    else:
        _warn(credentials_status.message)

    # 4) Test-cases workbook (prompt + remember on first run).
    cases = path_setup.resolve_required_path(
        key=TEST_CASES_KEY, label="deeplink test-cases workbook",
        default=test_cases_default, override=test_cases_override, kind="file",
        store=store, interactive=interactive, prompter=prompter,
    )
    if not cases.ok:
        return PreflightResult(False, message=cases.message)
    _log(cases.message)

    # 5) APK source (confirmed every interactive run) - decides what follows.
    source = apk_source_selector(
        store=store, default=apk_source_default,
        interactive=interactive, prompter=prompter,
    )
    if not source.ok:
        return PreflightResult(False, message=source.message)
    _log(source.message)

    # 6) Source-specific prerequisites. The enlistment + build toolchain are
    #    ONLY required when building locally, so a prebuilt-APK / Play Store run
    #    needs neither.
    resolved_enlistment: Optional[str] = None
    resolved_apk: Optional[str] = None
    if source.source == apk_source.BUILD:
        enlistment = path_setup.resolve_required_path(
            key=ENLISTMENT_KEY, label="Office Mobile enlistment",
            default=enlistment_default, override=enlistment_override, kind="dir",
            store=store, interactive=interactive, prompter=prompter,
        )
        if not enlistment.ok:
            return PreflightResult(False, message=enlistment.message)
        _log(enlistment.message)
        resolved_enlistment = enlistment.path

        build_tools_status = build_tools_checker()
        if not build_tools_status.ok:
            return PreflightResult(False, message=build_tools_status.message)
        _log(build_tools_status.message)
    elif source.source == apk_source.EXISTING:
        existing = path_setup.resolve_required_path(
            key=EXISTING_APK_KEY, label="prebuilt officemobile APK",
            default=None, kind="file", store=store,
            interactive=interactive, prompter=prompter,
        )
        if not existing.ok:
            return PreflightResult(False, message=existing.message)
        _log(existing.message)
        resolved_apk = existing.path
    # else: playstore - handled at install time (install from store if missing);
    #       no path/toolchain to resolve here.

    # 7) Device (with emulator auto-start when nothing is connected).
    device_status = _resolve_device(
        device, avd, autostart_emulator, device_checker, emulator_starter
    )
    if not device_status.ok:
        return PreflightResult(False, message=device_status.message)
    _log(device_status.message)

    return PreflightResult(
        True,
        device_id=device_status.device_id,
        test_cases_path=cases.path,
        apk_source=source.source,
        enlistment_root=resolved_enlistment,
        apk_path=resolved_apk,
        message="all prerequisites ready.",
    )
