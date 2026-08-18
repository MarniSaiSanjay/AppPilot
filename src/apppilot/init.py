"""AppPilot setup / readiness command (``/init``).

Framework-level, use-case-agnostic onboarding: probes prerequisites and model
configuration, collects every result, and finishes with a single readiness
summary. This module must never import from ``usecases/``.

The file is organised into clearly separated sections (result models, the
individual check groups, the readiness summary, and the CLI). Generic, reusable
system probing lives separately in :mod:`apppilot.environment`.

Phase 1 scope: environment prerequisites (Python, adb, Maestro, Java) and model
configuration. Device, APK and email setup are now integrated.
"""

from __future__ import annotations

import argparse
import enum
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from . import apk_config, device, device_config, email_delivery, environment

# `.agent` resolves within the top package in both import styles
# (`apppilot.agent` and `src.apppilot.agent`).
from .agent import _load_dotenv

try:  # `python -m src.apppilot_init` -> package is `src.apppilot`
    from ..shared.model_client import ChatModelClient
except ImportError:  # `python3 src/apppilot_init.py` -> `src` is on sys.path
    from shared.model_client import ChatModelClient  # type: ignore[no-redef]


# =============================================================================
# Result Models
# =============================================================================

class Importance(enum.Enum):
    """How much a check matters to overall readiness."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    INFORMATIONAL = "informational"


class Status(enum.Enum):
    """Outcome of an individual check."""

    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    NOT_CONFIGURED = "not_configured"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    """A single readiness finding.

    ``detail`` is the full human-readable line rendered after the status symbol
    (it already embeds the item name). ``remediation``, when set, is surfaced in
    the summary's "Required actions" list for blocking failures.
    """

    name: str
    group: str
    importance: Importance
    status: Status
    detail: str
    remediation: str = ""


_ENVIRONMENT = "Environment"
_MODEL = "Model"
_DEVICE = "Android Device"
_APK = "APK"
_EMAIL = "Email"


# =============================================================================
# Environment Checks
# =============================================================================

_MIN_PYTHON = (3, 11)


def check_python(version_info: Optional[Sequence[int]] = None) -> CheckResult:
    """Verify the running interpreter is Python 3.11+ (REQUIRED)."""
    vi = tuple(version_info) if version_info is not None else sys.version_info
    major, minor = vi[0], vi[1]
    micro = vi[2] if len(vi) > 2 else 0
    if (major, minor) >= _MIN_PYTHON:
        return CheckResult(
            "python", _ENVIRONMENT, Importance.REQUIRED, Status.PASS,
            f"Python {major}.{minor}.{micro}",
        )
    return CheckResult(
        "python", _ENVIRONMENT, Importance.REQUIRED, Status.FAIL,
        f"Python {major}.{minor}.{micro} (3.11+ required)",
        "Install Python 3.11 or newer.",
    )


def _check_required_tool(
    *,
    name: str,
    version_args: Sequence[str],
    label: str,
    version_prefix: str,
    remediation: str,
    which,
    runner,
) -> CheckResult:
    """Shared logic for a REQUIRED PATH tool (adb, Maestro).

    Missing -> FAIL (blocking). Present with a version -> PASS. Present but the
    version could not be read -> WARNING (usable, non-blocking).
    """
    probe = environment.probe_executable(
        name, version_args, which=which, runner=runner,
    )
    if not probe.found:
        return CheckResult(
            name, _ENVIRONMENT, Importance.REQUIRED, Status.FAIL,
            f"{label} not found", remediation,
        )
    if probe.version:
        return CheckResult(
            name, _ENVIRONMENT, Importance.REQUIRED, Status.PASS,
            f"{label} ({version_prefix}{probe.version})",
        )
    return CheckResult(
        name, _ENVIRONMENT, Importance.REQUIRED, Status.WARNING,
        f"{label} (installed, version unknown)",
    )


def check_adb(*, which=None, runner=None) -> CheckResult:
    """Verify ``adb`` is on PATH (REQUIRED)."""
    return _check_required_tool(
        name="adb",
        version_args=["version"],
        label="ADB",
        version_prefix="adb ",
        remediation="Install Android platform-tools and add adb to PATH.",
        which=which or shutil.which,
        runner=runner or subprocess.run,
    )


def check_maestro(*, which=None, runner=None) -> CheckResult:
    """Verify ``maestro`` is on PATH (REQUIRED)."""
    return _check_required_tool(
        name="maestro",
        version_args=["--version"],
        label="Maestro",
        version_prefix="",
        remediation="Install Maestro and add it to PATH "
                    "(https://maestro.mobile.dev).",
        which=which or shutil.which,
        runner=runner or subprocess.run,
    )


def check_java(*, which=None, runner=None) -> CheckResult:
    """Report Java availability (INFORMATIONAL - never blocks readiness)."""
    probe = environment.probe_executable(
        "java", ["-version"],
        which=which or shutil.which,
        runner=runner or subprocess.run,
    )
    if not probe.found:
        return CheckResult(
            "java", _ENVIRONMENT, Importance.INFORMATIONAL, Status.FAIL,
            "Java not found (informational)",
        )
    version = f" {probe.version}" if probe.version else ""
    return CheckResult(
        "java", _ENVIRONMENT, Importance.INFORMATIONAL, Status.PASS,
        f"Java{version} (informational)",
    )


# =============================================================================
# Model Checks
# =============================================================================

def _model_config_problem(config: Mapping[str, str]) -> Optional[str]:
    """Return a human-readable reason the model config is unusable, else None.

    Purely syntactic and offline: it verifies the values are well-formed enough
    to be usable - never contacting the endpoint, sending a prompt, or consuming
    tokens. ``config_from_env`` only checks that the model name and API key are
    present (non-empty), so whitespace-only values and a malformed base URL slip
    through as "configured". Those are configured-but-unusable, so /init must not
    report them as ready. The API key value is inspected but never echoed.
    """
    if not config["model"].strip():
        return "APPPILOT_MODEL is blank"
    if not config["api_key"].strip():
        return "APPPILOT_MODEL_API_KEY is blank"
    base_url = config["base_url"]
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return (
            f"APPPILOT_MODEL_BASE_URL is not a valid http(s) URL: {base_url!r}"
        )
    return None


def check_model(env: Optional[Mapping[str, str]] = None) -> CheckResult:
    """Verify model configuration is present and well-formed (REQUIRED).

    Reuses :meth:`ChatModelClient.config_from_env`. Reads only the model *name*
    for display - the API key is never included in the result. This is a
    configuration/readiness check, not a health check: it confirms the values
    are present and syntactically usable, but does NOT contact the model
    endpoint or consume tokens, so ``PASS`` means "configured", not "reachable".
    """
    source = os.environ if env is None else env
    config = ChatModelClient.config_from_env(source)
    if not config:
        return CheckResult(
            "model", _MODEL, Importance.REQUIRED, Status.NOT_CONFIGURED,
            "Model not configured",
            "Configure APPPILOT_MODEL and APPPILOT_MODEL_API_KEY.",
        )
    problem = _model_config_problem(config)
    if problem is not None:
        return CheckResult(
            "model", _MODEL, Importance.REQUIRED, Status.FAIL,
            f"Model misconfigured ({problem})",
            "Fix the model configuration: set a non-empty APPPILOT_MODEL and "
            "APPPILOT_MODEL_API_KEY, and a valid http(s) APPPILOT_MODEL_BASE_URL "
            "(e.g. https://api.openai.com/v1).",
        )
    return CheckResult(
        "model", _MODEL, Importance.REQUIRED, Status.PASS,
        f"Model configured (APPPILOT_MODEL={config['model']})",
    )


# =============================================================================
# Device Setup
# =============================================================================
#
# Interactive discovery/validation of the Android execution environment
# (physical device or emulator). Orchestration + UX only; the generic device
# capability lives in :mod:`apppilot.device`. Selection is for this run only -
# nothing is persisted and the runtime ``--device`` contract is unchanged.

_RUN_TARGET_PROMPT = (
    "How would you like to run AppPilot?\n\n"
    "  1. Physical Android device\n"
    "  2. Android Emulator\n"
)

# Exact approved copy shown when the emulator path finds no AVD.
_NO_AVD_MESSAGE = (
    "✗ No Android Emulator is configured.\n\n"
    "Please create an Android Virtual Device using Android Studio.\n\n"
    "After creating one, run /init again."
)


def _device_result(
    status: Status,
    detail: str,
    remediation: str = "",
    *,
    name: str = "device",
    importance: Importance = Importance.REQUIRED,
    serial: Optional[str] = None,
) -> CheckResult:
    # A PASS carries the resolved, already-validated (authorized + booted) serial,
    # which is persisted here so normal runtime can reuse the /init selection.
    # FAIL/SKIPPED paths pass no serial, so a bad device never overwrites a good
    # saved one. Persistence is best-effort and never blocks readiness.
    if status == Status.PASS and serial:
        device_config.save_device(serial)
    return CheckResult(name, _DEVICE, importance, status, detail, remediation)


def _ask(input_fn: Callable[[str], str], prompt: str) -> Optional[str]:
    """Prompt for input; EOF returns ``None``. Ctrl-C propagates to the caller."""
    try:
        return input_fn(prompt)
    except EOFError:
        return None


def _prompt_run_target(
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    *,
    max_attempts: int = 3,
) -> int:
    """Ask whether to target a physical device (1) or emulator (2). Default 2."""
    output(_RUN_TARGET_PROMPT)
    for _ in range(max_attempts):
        answer = _ask(input_fn, "Select [2]: ")
        if answer is None or not answer.strip():
            return 2
        answer = answer.strip()
        if answer in ("1", "2"):
            return int(answer)
        output("Please enter 1 or 2.")
    return 2


def _prompt_yes_no(
    question: str,
    *,
    default: bool,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    max_attempts: int = 3,
) -> bool:
    """Bounded yes/no prompt. Enter/EOF keeps the default."""
    hint = "[Y/n]" if default else "[y/N]"
    for _ in range(max_attempts):
        answer = _ask(input_fn, f"{question} {hint}: ")
        if answer is None:
            return default
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        output("Please answer y or n.")
    return default


def _select_from(
    items: Sequence,
    *,
    label: Callable[[object], str],
    header: str,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    max_attempts: int = 3,
):
    """Numbered selection among concrete items. Enter/EOF/invalid -> first item."""
    output(header)
    for index, item in enumerate(items, start=1):
        output(f"  {index}. {label(item)}")
    for _ in range(max_attempts):
        answer = _ask(input_fn, "Select [1]: ")
        if answer is None or not answer.strip():
            return items[0]
        answer = answer.strip()
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(items):
                return items[number - 1]
        output(f"Please enter a number between 1 and {len(items)}.")
    return items[0]


def _setup_physical(
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    runner,
    timeout: float,
) -> List[CheckResult]:
    """Discover, select and validate a physical device (authorized + booted)."""
    devices = [
        d for d in device.list_adb_devices(runner=runner, timeout=timeout)
        if not d.is_emulator
    ]
    if not devices:
        return [_device_result(
            Status.FAIL, "No physical device detected",
            "Connect an Android device over USB and enable USB debugging, "
            "then re-run /init.",
        )]

    authorized = [d for d in devices if d.is_authorized]
    if authorized:
        selected = authorized[0]
        if len(authorized) > 1 and interactive:
            selected = _select_from(
                authorized,
                label=lambda d: f"{d.serial}  ({d.state})",
                header="Multiple devices detected:",
                input_fn=input_fn, output=output,
            )
        if device.is_boot_completed(selected.serial, runner=runner, timeout=timeout):
            return [_device_result(
                Status.PASS, f"Physical device ready ({selected.serial})",
                serial=selected.serial)]
        return [_device_result(
            Status.FAIL, f"Physical device not fully booted ({selected.serial})",
            "Wait for the device to finish starting, then re-run /init.")]

    unauthorized = [d for d in devices if d.state == device.STATE_UNAUTHORIZED]
    if unauthorized:
        return [_device_result(
            Status.FAIL, f"Physical device unauthorized ({unauthorized[0].serial})",
            "Accept the USB debugging authorization prompt on the device, "
            "then re-run /init.")]
    offline = [d for d in devices if d.state == device.STATE_OFFLINE]
    if offline:
        return [_device_result(
            Status.FAIL, f"Physical device offline ({offline[0].serial})",
            "Reconnect the device or re-enable USB debugging, then re-run /init.")]
    return [_device_result(
        Status.FAIL, f"No authorized physical device ({devices[0].state})",
        "Ensure the device is connected and authorized, then re-run /init.")]


def _setup_emulator(
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    runner,
    timeout: float,
    popen,
    boot_timeout: float,
    poll_interval: float,
    clock,
    sleep,
    which,
) -> List[CheckResult]:
    """Detect tooling, discover/select an AVD, and validate/boot the emulator."""
    tooling = environment.probe_executable(
        "emulator", ["-version"], which=which, runner=runner,
    )
    if not tooling.found:
        return [
            _device_result(
                Status.FAIL, "Emulator tooling not found",
                "Install the Android SDK emulator and add it to PATH "
                "($ANDROID_HOME/emulator).",
                name="emulator_tooling"),
            _device_result(
                Status.SKIPPED, "AVD readiness (emulator tooling missing)",
                name="avd"),
        ]

    avds = device.list_avds(runner=runner, timeout=timeout)
    if not avds:
        output(_NO_AVD_MESSAGE)
        return [_device_result(
            Status.FAIL, "No Android Emulator configured",
            "Create an Android Virtual Device using Android Studio, "
            "then re-run /init.", name="avd")]

    selected = avds[0]
    if len(avds) > 1 and interactive:
        selected = _select_from(
            avds, label=lambda a: str(a),
            header="Multiple emulators found:",
            input_fn=input_fn, output=output,
        )

    running = device.find_running_avd(selected, runner=runner, timeout=timeout)
    if running is not None:
        if device.is_boot_completed(running.serial, runner=runner, timeout=timeout):
            return [_device_result(
                Status.PASS,
                f'Emulator "{selected}" running and booted ({running.serial})',
                name="avd", serial=running.serial)]
        output(f'Emulator "{selected}" is starting; waiting for boot...')
        if device.wait_for_boot(
            running.serial, runner=runner, timeout=boot_timeout,
            poll_interval=poll_interval, clock=clock, sleep=sleep,
        ):
            return [_device_result(
                Status.PASS, f'Emulator "{selected}" booted ({running.serial})',
                name="avd", serial=running.serial)]
        return [_device_result(
            Status.FAIL, f'Emulator "{selected}" did not finish booting',
            "Start the emulator from Android Studio and wait for it to boot, "
            "then re-run /init.", name="avd")]

    if not interactive:
        return [_device_result(
            Status.FAIL, f'Emulator "{selected}" is not running',
            "Start the emulator, then re-run /init.", name="avd")]

    start = _prompt_yes_no(
        f'Emulator "{selected}" is not running.\nStart it now?',
        default=True, input_fn=input_fn, output=output,
    )
    if not start:
        return [_device_result(
            Status.FAIL, f'Emulator "{selected}" is not running',
            "Start the emulator, then re-run /init.", name="avd")]
    if not device.start_emulator(selected, popen=popen):
        return [_device_result(
            Status.FAIL, f'Could not launch emulator "{selected}"',
            "Start the emulator from Android Studio, then re-run /init.",
            name="avd")]
    output(f'Starting emulator "{selected}"; waiting for boot...')
    serial = device.wait_for_emulator(
        selected, runner=runner, timeout=boot_timeout,
        poll_interval=poll_interval, clock=clock, sleep=sleep,
    )
    if serial is None:
        return [_device_result(
            Status.FAIL, f'Emulator "{selected}" did not finish booting in time',
            "Ensure the emulator can start, then re-run /init.", name="avd")]
    return [_device_result(
        Status.PASS, f'Emulator "{selected}" started and booted ({serial})',
        name="avd", serial=serial)]


def _setup_device_noninteractive(*, runner, timeout: float) -> List[CheckResult]:
    """Auto-detect without prompting or starting anything (non-interactive).

    Never asks physical-vs-emulator and never starts an emulator; reports the
    first authorized, booted device or a clear not-ready action.
    """
    devices = device.list_adb_devices(runner=runner, timeout=timeout)
    for d in devices:
        if d.is_authorized and device.is_boot_completed(
            d.serial, runner=runner, timeout=timeout,
        ):
            return [_device_result(Status.PASS, f"Device ready ({d.serial})",
                                   serial=d.serial)]
    if any(d.is_authorized for d in devices):
        return [_device_result(
            Status.FAIL, "Authorized device not fully booted",
            "Wait for the device/emulator to finish booting, then re-run /init.")]
    return [_device_result(
        Status.FAIL, "No authorized device available",
        "Connect and authorize an Android device, or start an emulator, "
        "then re-run /init.")]


def setup_device(
    *,
    adb_available: bool,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    runner=None,
    timeout: float = device.DEFAULT_ADB_TIMEOUT,
    popen=subprocess.Popen,
    boot_timeout: float = device.DEFAULT_BOOT_TIMEOUT,
    poll_interval: float = device.DEFAULT_POLL_INTERVAL,
    clock=time.monotonic,
    sleep=time.sleep,
    which=None,
) -> List[CheckResult]:
    """Run the Android device readiness setup and return its check results.

    Collect-all: always returns results (never raises for a device problem).
    When adb itself is unavailable, device checks are SKIPPED (non-blocking) -
    adb's own REQUIRED failure already owns that remediation.
    """
    runner = runner or subprocess.run
    which = which or shutil.which
    if not adb_available:
        return [_device_result(
            Status.SKIPPED, "Device checks skipped (adb unavailable)")]
    if not interactive:
        return _setup_device_noninteractive(runner=runner, timeout=timeout)

    target = _prompt_run_target(input_fn, output)
    if target == 1:
        return _setup_physical(
            interactive=interactive, input_fn=input_fn, output=output,
            runner=runner, timeout=timeout,
        )
    return _setup_emulator(
        interactive=interactive, input_fn=input_fn, output=output, runner=runner,
        timeout=timeout, popen=popen, boot_timeout=boot_timeout,
        poll_interval=poll_interval, clock=clock, sleep=sleep, which=which,
    )


# =============================================================================
# APK Configuration
# =============================================================================
#
# Readiness wrapper around the existing generic APK capability
# (:mod:`apppilot.apk_config`). ``resolve_apk_path`` is the single APK
# mechanism - it owns validation, the bracketed-default interactive prompt, and
# persistence of the last good path. This section only maps its result onto a
# ``CheckResult`` for the readiness summary; it adds no new prompting,
# persistence, or validation.


def check_apk(
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    resolve: Callable = apk_config.resolve_apk_path,
) -> CheckResult:
    """Resolve/validate the local APK and map it to a REQUIRED readiness check.

    Reuses :func:`apppilot.apk_config.resolve_apk_path` exactly: interactive runs
    get the ``APK path [<saved>]:`` prompt + persistence, non-interactive runs
    reuse a saved, still-valid path only. In non-interactive mode the resolver's
    runtime-flavoured inline messages are suppressed so the readiness summary
    owns the user-facing remediation.

    Collect-all: never raises. ``resolve_apk_path`` is already exception-safe,
    but any unexpected error is defensively turned into a clean REQUIRED FAIL so
    every other check still reports.
    """
    sink = output if interactive else (lambda _message: None)
    try:
        path = resolve(None, interactive=interactive, input_fn=input_fn, output=sink)
    except Exception:  # defensive collect-all guard; never surface a traceback
        path = None
    if path is None:
        return CheckResult(
            "apk", _APK, Importance.REQUIRED, Status.FAIL,
            "No valid APK configured",
            "Configure a valid APK (run /init and enter the path, or pass "
            "--apk to the run command).",
        )
    return CheckResult(
        "apk", _APK, Importance.REQUIRED, Status.PASS,
        f"APK configured\n    {path}",
    )


# =============================================================================
# Email Configuration
# =============================================================================
#
# Optional readiness wrapper around the existing generic email capability
# (:mod:`apppilot.email_delivery`). ``configure_recipient`` is the single,
# shared recipient mechanism - it owns the saved-recipient store, validation,
# the bracketed-default prompt, Enter-keeps/replace behaviour and EOF/Ctrl-C
# handling (the same helper the normal-run send flow uses). This section only
# gates it behind an opt-in question and maps the outcome onto an OPTIONAL
# ``CheckResult``. It NEVER sends an email or makes a network request, and email
# never blocks readiness. Missing relay config is surfaced as a non-blocking
# WARNING reporting only the missing key NAMES (never any secret value).


def _email_result(recipient: Optional[str], env: Mapping[str, str]) -> CheckResult:
    """Map a resolved recipient (+ relay config presence) to an OPTIONAL result."""
    if recipient is None:
        return CheckResult(
            "email", _EMAIL, Importance.OPTIONAL, Status.NOT_CONFIGURED,
            "Email not configured (optional)",
        )
    missing = email_delivery._missing_config(env)
    if missing:
        return CheckResult(
            "email", _EMAIL, Importance.OPTIONAL, Status.WARNING,
            "Email reporting not fully configured\n"
            f"    Configure {', '.join(missing)}",
        )
    return CheckResult(
        "email", _EMAIL, Importance.OPTIONAL, Status.PASS,
        f"Email configured\n    {recipient}",
    )


def check_email(
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    env: Optional[Mapping[str, str]] = None,
    configure: Callable = email_delivery.configure_recipient,
    load_saved: Callable[[], Optional[str]] = email_delivery._load_saved_recipient,
) -> CheckResult:
    """Optionally configure email reporting and map it to an OPTIONAL check.

    Email is never REQUIRED, so it can never make ``/init`` NOT ready. When a
    recipient was saved on a previous run it is offered as the default straight
    away (Enter keeps it) - matching ``./run`` - so the saved address is never
    silently skipped. Only when nothing is saved does the interactive run first
    ask ``Configure email reporting? [y/N]`` (default No). Non-interactive runs
    never prompt and simply reuse a saved recipient if one exists. ``/init``
    NEVER sends an email or contacts the relay - it only inspects local config.

    Collect-all: never raises on error - any unexpected failure becomes a clean
    OPTIONAL "not configured" result so every other check still reports. A
    genuine Ctrl-C (KeyboardInterrupt) is NOT swallowed here; it propagates so
    ``/init`` aborts consistently instead of reporting a misleading result.
    """
    resolved_env = os.environ if env is None else env
    try:
        if not interactive:
            outcome = configure(interactive=False)
            return _email_result(outcome.recipient, resolved_env)

        # With no saved recipient, gate behind a y/N so fresh setups can decline;
        # with one saved, skip the gate and let ``configure`` offer it as default.
        if load_saved() is None and not _prompt_yes_no(
            "Configure email reporting?",
            default=False,
            input_fn=input_fn,
            output=output,
        ):
            return CheckResult(
                "email", _EMAIL, Importance.OPTIONAL, Status.NOT_CONFIGURED,
                "Email skipped (optional)",
            )

        outcome = configure(interactive=True, input_fn=input_fn, output=output)
        if outcome.recipient is None:
            detail = (
                "Email skipped (optional)" if outcome.cancelled
                else "Email not configured (optional)"
            )
            return CheckResult(
                "email", _EMAIL, Importance.OPTIONAL, Status.NOT_CONFIGURED, detail,
            )
        return _email_result(outcome.recipient, resolved_env)
    except Exception:
        # Collect-all for ERRORS only: an email-config failure becomes a clean
        # OPTIONAL "not configured" so every other check still reports. A genuine
        # Ctrl-C (KeyboardInterrupt) is intentionally NOT caught here - it
        # propagates to main()'s handler so /init aborts ("Aborted." / exit 130)
        # instead of being silently turned into a "not configured, ready" result.
        return CheckResult(
            "email", _EMAIL, Importance.OPTIONAL, Status.NOT_CONFIGURED,
            "Email not configured (optional)",
        )


# =============================================================================
# Readiness Summary
# =============================================================================

# Status symbols (design-approved):
#   ✓ pass   ✗ required missing   ○ optional/informational   ! warning/degraded
_PASS = "✓"
_FAIL = "✗"
_INFO = "○"
_WARN = "!"

_SEPARATOR = "─" * 29


def _symbol(result: CheckResult) -> str:
    """Map a result to its display symbol."""
    if result.status == Status.WARNING:
        return _WARN
    if result.status == Status.SKIPPED:
        return _INFO
    if result.importance == Importance.INFORMATIONAL:
        return _INFO
    if result.status == Status.PASS:
        return _PASS
    if result.status == Status.NOT_CONFIGURED \
            and result.importance == Importance.OPTIONAL:
        return _INFO
    return _FAIL


def _blocks_readiness(result: CheckResult) -> bool:
    """A check blocks readiness only if it is REQUIRED and failed/unconfigured.

    A REQUIRED tool that is present but version-unknown (WARNING) is usable and
    does not block. OPTIONAL/INFORMATIONAL items never block.
    """
    return (
        result.importance == Importance.REQUIRED
        and result.status in (Status.FAIL, Status.NOT_CONFIGURED)
    )


def _ordered_groups(results: Sequence[CheckResult]) -> List[str]:
    """Group names in first-seen order."""
    groups: List[str] = []
    for result in results:
        if result.group not in groups:
            groups.append(result.group)
    return groups


def render(results: Sequence[CheckResult]) -> Tuple[str, int]:
    """Render the readiness summary and return ``(text, exit_code)``.

    ``exit_code`` is 0 when every REQUIRED check passes, otherwise 1.
    """
    lines: List[str] = ["AppPilot Setup", ""]

    for group in _ordered_groups(results):
        lines.append(group)
        for result in results:
            if result.group == group:
                lines.append(f"  {_symbol(result)} {result.detail}")
        lines.append("")

    lines.append(_SEPARATOR)
    lines.append("")

    blocking = [r for r in results if _blocks_readiness(r)]
    if not blocking:
        lines.append("AppPilot is ready.")
        return "\n".join(lines), 0

    lines.append("AppPilot is NOT ready.")
    lines.append("")
    lines.append("Required actions:")
    for index, result in enumerate(blocking, start=1):
        action = result.remediation or f"Resolve: {result.detail}"
        lines.append(f"{index}. {action}")
    lines.append("")
    lines.append("Run /init again after completing them.")
    return "\n".join(lines), 1


# =============================================================================
# CLI
# =============================================================================

_DESCRIPTION = (
    "Check whether this machine is ready to run AppPilot and print a "
    "readiness summary."
)


def run_all_checks(
    *,
    which=None,
    runner=None,
    env: Optional[Mapping[str, str]] = None,
    version_info: Optional[Sequence[int]] = None,
) -> List[CheckResult]:
    """Run every Phase 1 check and return all results in display order.

    Collect-all: a failure in one check never prevents the others from running.
    """
    which = which or shutil.which
    runner = runner or subprocess.run
    return [
        check_python(version_info),
        check_adb(which=which, runner=runner),
        check_maestro(which=which, runner=runner),
        check_java(which=which, runner=runner),
        check_model(env),
    ]


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="apppilot init", description=_DESCRIPTION)
    return parser.parse_args(argv)


def _is_interactive() -> bool:
    return getattr(sys.stdin, "isatty", lambda: False)()


def _adb_available(core: Sequence[CheckResult]) -> bool:
    """Whether adb is usable (found), from the environment check results.

    A present-but-version-unknown adb (WARNING) is still usable; only a missing
    adb (FAIL) makes device checks impossible.
    """
    adb = next((r for r in core if r.name == "adb"), None)
    return adb is not None and adb.status in (Status.PASS, Status.WARNING)


def _compose_results(
    core: Sequence[CheckResult],
    device_results: Sequence[CheckResult],
    apk_result: CheckResult,
    email_result: CheckResult,
) -> List[CheckResult]:
    """Order groups as Environment -> Model -> Android Device -> APK -> Email."""
    environment_group = [r for r in core if r.group == _ENVIRONMENT]
    model_group = [r for r in core if r.group == _MODEL]
    return [
        *environment_group, *model_group, *device_results,
        apk_result, email_result,
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run ``/init`` and return the process exit code (0 ready, 1 not ready).

    ``argparse`` handles ``--help`` (exit 0) and usage errors (exit 2) by
    raising ``SystemExit`` before any checks run.
    """
    _parse_args(argv)
    try:
        _load_dotenv()
        core = run_all_checks()
        device_results = setup_device(
            adb_available=_adb_available(core),
            interactive=_is_interactive(),
        )
        apk_result = check_apk(interactive=_is_interactive())
        email_result = check_email(interactive=_is_interactive())
        results = _compose_results(core, device_results, apk_result, email_result)
        text, exit_code = render(results)
        print(text)
        return exit_code
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 130
