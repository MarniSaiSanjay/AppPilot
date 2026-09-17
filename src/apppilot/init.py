"""AppPilot setup / readiness command."""

from __future__ import annotations

import argparse
import enum
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from . import (
    apk_config,
    bootstrap,
    device,
    device_config,
    email_delivery,
    environment,
)
from .agent import _load_dotenv
try:
    from ..shared.entra_auth import (
        ensure_entra_model_readiness,
        ensure_relay_environment,
    )
except ImportError:
    from shared.entra_auth import (  # type: ignore[no-redef]
        ensure_entra_model_readiness,
        ensure_relay_environment,
    )

try:
    from ..shared.model_client import (
        ChatModelClient,
        ModelConfigurationError,
        validate_base_url,
    )
except ImportError:
    from shared.model_client import (  # type: ignore[no-redef]
        ChatModelClient,
        ModelConfigurationError,
        validate_base_url,
    )


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
    """A single readiness finding."""

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
    """Check a required PATH tool."""
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
    """Verify Java 17+ availability."""
    which = which or shutil.which
    if not which("java"):
        return CheckResult(
            "java", _ENVIRONMENT, Importance.REQUIRED, Status.FAIL,
            "Java not found",
            "Install Java 17+ and add java to PATH; Maestro requires it.",
        )
    ok, output = environment.run(
        ["java", "-version"],
        runner=runner or subprocess.run,
    )
    match = re.search(r'\bversion\s+"([^"]+)"', output, re.IGNORECASE)
    version = match.group(1) if ok and match else None
    if not version or not re.fullmatch(
        r"\d+(?:\.\d+)*(?:_\d+)?(?:[-+][0-9A-Za-z.-]+)?",
        version,
    ):
        return CheckResult(
            "java", _ENVIRONMENT, Importance.REQUIRED, Status.FAIL,
            "Java version could not be determined",
            "Install Java 17+ and ensure `java -version` reports its version.",
        )
    parts = version.split(".")
    try:
        major = int(parts[1] if parts[0] == "1" and len(parts) > 1 else parts[0])
    except ValueError:
        major = 0
    if major < 17:
        return CheckResult(
            "java", _ENVIRONMENT, Importance.REQUIRED, Status.FAIL,
            f"Java {version} is unsupported",
            "Install Java 17 or newer and make it the active java on PATH.",
        )
    return CheckResult(
        "java", _ENVIRONMENT, Importance.REQUIRED, Status.PASS,
        f"Java {version}",
    )


def check_azure_cli(*, which=None, runner=None) -> CheckResult:
    """Verify Azure CLI is available for work-account sign-in."""
    return _check_required_tool(
        name="az",
        version_args=["version"],
        label="Azure CLI",
        version_prefix="",
        remediation="Install Azure CLI, then re-run /setup.",
        which=which or shutil.which,
        runner=runner or subprocess.run,
    )


def _model_config_problem(config: Mapping[str, str]) -> Optional[str]:
    """Return a human-readable reason the model config is unusable."""
    if not config["model"].strip():
        return "APPPILOT_MODEL is blank"
    try:
        validate_base_url(config["base_url"])
    except ModelConfigurationError as error:
        return str(error)
    return None


def check_model(env: Optional[Mapping[str, str]] = None) -> CheckResult:
    """Verify model configuration is present and well-formed."""
    source = os.environ if env is None else env
    try:
        config = ChatModelClient.config_from_env(source)
    except ModelConfigurationError as error:
        return CheckResult(
            "model", _MODEL, Importance.REQUIRED, Status.FAIL,
            f"Model misconfigured ({error})",
            "Use HTTPS for remote model endpoints. HTTP is allowed only for "
            "explicit loopback development endpoints.",
        )
    if not config:
        return CheckResult(
            "model", _MODEL, Importance.REQUIRED, Status.NOT_CONFIGURED,
            "Model not configured",
            "AppPilot model configuration is unavailable. Update AppPilot and retry.",
        )
    problem = _model_config_problem(config)
    if problem is not None:
        return CheckResult(
            "model", _MODEL, Importance.REQUIRED, Status.FAIL,
            f"Model misconfigured ({problem})",
            "AppPilot model configuration is invalid. Update AppPilot and retry.",
        )
    return CheckResult(
        "model", _MODEL, Importance.REQUIRED, Status.PASS,
        f"Work Azure OpenAI model configured (APPPILOT_MODEL={config['model']})",
    )


def check_entra_model_authentication(
    env: Optional[Mapping[str, str]] = None,
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> Optional[CheckResult]:
    """Verify Entra token readiness and offer Azure CLI login when appropriate."""
    source = os.environ if env is None else env
    try:
        readiness = ensure_entra_model_readiness(
            env=source,
            interactive=interactive,
            input_fn=input_fn,
            output=output,
        )
    except ModelConfigurationError:
        return None
    if not readiness.required:
        return None
    if readiness.ready:
        return CheckResult(
            "entra-auth",
            _MODEL,
            Importance.REQUIRED,
            Status.PASS,
            "Microsoft Entra model authentication ready",
        )
    return CheckResult(
        "entra-auth",
        _MODEL,
        Importance.REQUIRED,
        Status.FAIL,
        "Microsoft Entra model authentication unavailable",
        readiness.detail,
    )


_RUN_TARGET_PROMPT = (
    "How would you like to run AppPilot?\n\n"
    "  1. Physical Android device\n"
    "  2. Android Emulator\n"
)

_NO_AVD_MESSAGE = (
    "✗ No Android emulator is configured. Add one and try again."
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
            "Add an Android emulator or select a physical device, "
            "then try /init again.", name="avd")]

    selected = avds[0]
    if len(avds) > 1 and interactive:
        selected = _select_from(
            avds, label=lambda a: str(a),
            header="Multiple emulators found:",
            input_fn=input_fn, output=output,
        )

    running = device.find_running_avd(
        selected,
        runner=runner,
        timeout=timeout,
        allow_unique_fallback=len(avds) == 1,
    )
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
    """Auto-detect a device without prompting."""
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
    """Run Android device readiness setup."""
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


def check_apk(
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    resolve: Callable = apk_config.resolve_apk_path,
) -> CheckResult:
    """Resolve the local APK and map it to a readiness check."""
    sink = output if interactive else (lambda _message: None)
    try:
        path = resolve(None, interactive=interactive, input_fn=input_fn, output=sink)
    except Exception:
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


def _email_result(recipient: Optional[str], env: Mapping[str, str]) -> CheckResult:
    """Map recipient and relay config to an email readiness result."""
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
    """Optionally configure email reporting."""
    resolved_env = os.environ if env is None else env
    try:
        if not interactive:
            outcome = configure(interactive=False)
            return _email_result(outcome.recipient, resolved_env)

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
        return CheckResult(
            "email", _EMAIL, Importance.OPTIONAL, Status.NOT_CONFIGURED,
            "Email not configured (optional)",
        )


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
    """Return whether a check blocks readiness."""
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


def render(
    results: Sequence[CheckResult],
    *,
    retry_command: str = "/init",
) -> Tuple[str, int]:
    """Render the readiness summary and return ``(text, exit_code)``."""
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
    lines.append(f"Run {retry_command} again after completing them.")
    return "\n".join(lines), 1


_DESCRIPTION = (
    "Configure AppPilot's model, device, APK, and optional email report settings."
)


def run_all_checks(
    *,
    which=None,
    runner=None,
    env: Optional[Mapping[str, str]] = None,
    version_info: Optional[Sequence[int]] = None,
) -> List[CheckResult]:
    """Run readiness checks in display order."""
    return [
        *run_environment_checks(
            which=which,
            runner=runner,
            env=env,
            version_info=version_info,
        ),
        check_model(env),
    ]


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="apppilot init", description=_DESCRIPTION)
    return parser.parse_args(argv)


def run_environment_checks(
    *,
    which=None,
    runner=None,
    env: Optional[Mapping[str, str]] = None,
    version_info: Optional[Sequence[int]] = None,
) -> List[CheckResult]:
    """Run the local-machine prerequisite checks used by ``/setup``."""
    which = which or shutil.which
    runner = runner or subprocess.run
    return [
        check_python(version_info),
        check_adb(which=which, runner=runner),
        check_maestro(which=which, runner=runner),
        check_java(which=which, runner=runner),
        check_azure_cli(which=which, runner=runner),
    ]


def _is_interactive() -> bool:
    return getattr(sys.stdin, "isatty", lambda: False)()


def _adb_available(core: Sequence[CheckResult]) -> bool:
    """Return whether adb is usable."""
    adb = next((r for r in core if r.name == "adb"), None)
    return adb is not None and adb.status in (Status.PASS, Status.WARNING)


def _bootstrap_missing(core: Sequence[CheckResult]) -> list[str]:
    """Return readiness check names that the local bootstrap can address."""
    missing = [
        result.name
        for result in core
        if result.status == Status.FAIL
        and result.name in {"adb", "az", "java", "maestro"}
    ]
    repository = Path(__file__).resolve().parents[2]
    if not bootstrap._venv_python(repository, platform.system()).is_file():
        missing.append("virtual-environment")
    return missing


def _offer_bootstrap(
    core: Sequence[CheckResult],
    *,
    requested: bool,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    repository: Optional[Path] = None,
) -> bool:
    """Show the machine-change plan and run it only after explicit approval."""
    missing = _bootstrap_missing(core)
    repo = repository or Path(__file__).resolve().parents[2]
    plan = bootstrap.create_plan(
        missing=missing,
        repository=repo,
        env=dict(os.environ),
    )
    if not plan.actions:
        return False
    if not requested and not interactive:
        return False
    output(bootstrap.render_plan(plan))
    if not interactive or not _prompt_yes_no(
        "Apply this setup plan?",
        default=False,
        input_fn=input_fn,
        output=output,
    ):
        return False
    outcomes = bootstrap.apply_plan(plan, output=output)
    environment.add_java_path()
    failed = next((outcome for outcome in outcomes if not outcome.succeeded), None)
    if failed is not None:
        output(f"Setup stopped while installing: {failed.action.description}")
    else:
        output("Setup actions completed. Re-checking readiness...")
    return True


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


def setup_main() -> int:
    """Install and verify AppPilot's local machine prerequisites."""
    try:
        _load_dotenv()
        core = run_environment_checks()
        interactive = _is_interactive()
        if _offer_bootstrap(
            core,
            requested=True,
            interactive=interactive,
        ):
            core = run_environment_checks()
        text, exit_code = render(core, retry_command="/setup")
        print(text)
        return exit_code
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 130


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Configure AppPilot's model, device, APK, and optional email settings."""
    _parse_args(argv)
    try:
        _load_dotenv()
        adb_result = check_adb()
        core = [check_model()]
        interactive = _is_interactive()
        entra_result = check_entra_model_authentication(
            interactive=interactive
        )
        if entra_result is not None:
            core.append(entra_result)
        device_results = setup_device(
            adb_available=adb_result.status in (Status.PASS, Status.WARNING),
            interactive=interactive,
        )
        apk_result = check_apk(interactive=interactive)
        relay_env, relay_detail = ensure_relay_environment(
            os.environ, interactive=interactive
        )
        if relay_detail:
            email_result = CheckResult(
                "email",
                _EMAIL,
                Importance.OPTIONAL,
                Status.WARNING,
                "AppPilot email relay configuration unavailable",
                relay_detail,
            )
        else:
            email_result = check_email(
                interactive=interactive,
                env=relay_env,
            )
        results = _compose_results(core, device_results, apk_result, email_result)
        text, exit_code = render(results)
        print(text)
        return exit_code
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 130
