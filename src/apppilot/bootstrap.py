"""Consent-based local prerequisite bootstrap for AppPilot."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import environment

@dataclass(frozen=True)
class BootstrapAction:
    """One explicit local machine change offered by ``/init``."""

    name: str
    description: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class BootstrapPlan:
    """The supported prerequisite changes for the current machine."""

    actions: tuple[BootstrapAction, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BootstrapOutcome:
    """The result of running a single bootstrap action."""

    action: BootstrapAction
    succeeded: bool


def _sdk_root(env: dict[str, str] | None = None) -> Path | None:
    return environment.android_sdk_root(env=env)


def _sdkmanager_path(root: Path | None) -> str | None:
    if root is None:
        return None
    candidates = sorted(
        (
            *(root / "cmdline-tools").glob("*/bin/sdkmanager"),
            *(root / "cmdline-tools").glob("*/bin/sdkmanager.bat"),
        )
    )
    for candidate in reversed(candidates):
        if candidate.is_file() and (
            candidate.suffix.lower() == ".bat" or candidate.stat().st_mode & 0o111
        ):
            return str(candidate)
    return None


def _sdk_root_for_manager(manager: str) -> Path | None:
    """Infer an SDK root from ``cmdline-tools/<version>/bin/sdkmanager``."""
    try:
        path = Path(manager).resolve(strict=True)
    except OSError:
        return None
    if path.name not in {"sdkmanager", "sdkmanager.bat"} or len(path.parents) < 4:
        return None
    root = path.parents[3]
    return root if (root / "cmdline-tools").is_dir() else None


def _maestro_shell(
    operating_system: str,
    which: Callable[[str], str | None],
) -> str | None:
    """Return Bash usable by the Maestro installer, excluding the WSL launcher."""
    shell = which("bash")
    if operating_system != "Windows":
        return shell
    if shell is None:
        return environment.git_bash_path()
    normalized = shell.replace("/", "\\").lower()
    if normalized.endswith("\\windows\\system32\\bash.exe"):
        return environment.git_bash_path()
    return shell


def create_plan(
    *,
    missing: Iterable[str],
    repository: Path,
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    system: str | None = None,
) -> BootstrapPlan:
    """Return safe, supported actions for the missing readiness checks.

    AppPilot intentionally does not download Android system images or create an
    AVD. An emulator remains an explicit user-managed testing target.
    """
    missing_names = set(missing)
    if not missing_names:
        return BootstrapPlan(())

    operating_system = system or platform.system()
    if operating_system not in {"Darwin", "Windows"}:
        return BootstrapPlan(
            (),
            (
                "Automatic prerequisite installation is currently supported on "
                "macOS and Windows only.",
            ),
        )

    actions: list[BootstrapAction] = []
    notes: list[str] = []
    package_manager = which("brew" if operating_system == "Darwin" else "winget")
    if "java" in missing_names:
        if operating_system == "Darwin" and package_manager:
            actions.append(BootstrapAction(
                "java",
                "Install Java 17 for Maestro.",
                (package_manager, "install", "--cask", "temurin@17"),
            ))
        elif operating_system == "Windows" and package_manager:
            actions.append(BootstrapAction(
                "java",
                "Install Java 17 for Maestro.",
                (
                    package_manager, "install", "--id", "EclipseAdoptium.Temurin.17.JDK",
                    "--exact", "--accept-source-agreements",
                    "--accept-package-agreements",
                ),
            ))
        else:
            manager = "Homebrew" if operating_system == "Darwin" else "WinGet"
            notes.append(f"Java 17 is missing. Install {manager} first, then re-run /setup.")

    if "maestro" in missing_names and "java" in missing_names:
        notes.append(
            "Re-run /setup after Java installs to install the Maestro CLI."
        )
    elif "maestro" in missing_names:
        bash = _maestro_shell(operating_system, which)
        if bash:
            actions.append(BootstrapAction(
                "maestro",
                "Install the Maestro CLI using its official installer.",
                ("maestro-installer", bash),
            ))
        elif operating_system == "Windows" and package_manager:
            actions.append(BootstrapAction(
                "git-bash",
                "Install Git Bash for this Windows user, required by Maestro.",
                (
                    package_manager, "install", "--id", "Git.Git", "--exact",
                    "--scope", "user",
                    "--accept-source-agreements", "--accept-package-agreements",
                ),
            ))
            notes.append(
                "Re-run /setup after Git Bash installs to install the Maestro CLI."
            )
        else:
            notes.append(
                "Maestro is missing. Install Bash, then re-run /setup to use the "
                "official Maestro installer."
            )

    android_missing = {"adb", "apk-tools"} & missing_names
    if android_missing:
        root = _sdk_root(env)
        sdkmanager = _sdkmanager_path(root)
        if sdkmanager is None and (manager_on_path := which("sdkmanager")):
            root = _sdk_root_for_manager(manager_on_path)
            sdkmanager = manager_on_path if root is not None else None
        if sdkmanager and root:
            actions.append(BootstrapAction(
                "android-sdk",
                "Install Android platform, emulator, and APK-inspection tools.",
                (
                    sdkmanager,
                    f"--sdk_root={root}",
                    "platform-tools",
                    "emulator",
                    "build-tools;36.1.0",
                ),
            ))
            actions.append(BootstrapAction(
                "android-licenses",
                "Review and accept Android SDK licenses.",
                (sdkmanager, f"--sdk_root={root}", "--licenses"),
            ))
        elif operating_system == "Darwin" and package_manager:
            actions.append(BootstrapAction(
                "android-commandline-tools",
                "Install Android SDK command-line tools.",
                (package_manager, "install", "--cask", "android-commandlinetools"),
            ))
            notes.append(
                "Re-run /setup after command-line tools install to install Android SDK packages."
            )
        elif operating_system == "Windows" and package_manager:
            actions.append(BootstrapAction(
                "android-studio",
                "Install Android Studio, including the Android SDK manager.",
                (
                    package_manager, "install", "--id", "Google.AndroidStudio",
                    "--exact", "--accept-source-agreements",
                    "--accept-package-agreements",
                ),
            ))
            notes.append(
                "Open Android Studio once to install its command-line tools, then re-run /setup."
            )
        else:
            notes.append(
                "Android tools are missing. Install the Android command-line tools, "
                "then re-run /setup."
            )

    virtualenv = _venv_python(repository, operating_system)
    if not virtualenv.is_file():
        actions.extend((
            BootstrapAction(
                "virtual-environment",
                "Create AppPilot's isolated Python environment.",
                (sys.executable, "-m", "venv", str(repository / ".venv")),
            ),
            BootstrapAction(
                "python-dependencies",
                "Install AppPilot runtime dependencies into .venv.",
                (
                    str(virtualenv),
                    "-m",
                    "pip",
                    "install",
                    "-e",
                    str(repository),
                ),
            ),
        ))
    return BootstrapPlan(tuple(actions), tuple(notes))


def _venv_python(repository: Path, operating_system: str) -> Path:
    """Return the platform-specific virtual-environment interpreter path."""
    if operating_system == "Windows":
        return repository / ".venv" / "Scripts" / "python.exe"
    return repository / ".venv" / "bin" / "python"


def render_plan(plan: BootstrapPlan) -> str:
    """Render a reviewable setup plan before changing the machine."""
    lines = ["AppPilot can install the following prerequisites:"]
    for action in plan.actions:
        command = (
            "curl -fsSL https://get.maestro.mobile.dev -o <temporary installer>"
            f" && {action.command[1]} <temporary installer>"
            if action.command[0] == "maestro-installer"
            else " ".join(action.command)
        )
        lines.append(f"  - {action.description}")
        lines.append(f"    {command}")
    lines.extend(f"Note: {note}" for note in plan.notes)
    return "\n".join(lines)


def apply_plan(
    plan: BootstrapPlan,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    output: Callable[[str], None] = print,
) -> tuple[BootstrapOutcome, ...]:
    """Run approved actions in order and stop when one fails."""
    outcomes: list[BootstrapOutcome] = []
    for action in plan.actions:
        if action.command[0] == "maestro-installer":
            succeeded = _install_maestro(runner, action.command[1], output=output)
        else:
            result = runner(list(action.command), check=False)
            succeeded = result.returncode == 0
        outcomes.append(BootstrapOutcome(action, succeeded))
        if not succeeded:
            break
    return tuple(outcomes)


def _install_maestro(
    runner: Callable[..., subprocess.CompletedProcess],
    shell: str,
    *,
    output: Callable[[str], None] = print,
) -> bool:
    """Download the official Maestro installer to a temporary file before running it."""
    with tempfile.TemporaryDirectory(prefix="apppilot-maestro-") as temporary:
        installer = Path(temporary) / "install-maestro.sh"
        downloaded = runner(
            [
                "curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--output",
                str(installer),
                "https://get.maestro.mobile.dev",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if downloaded.returncode != 0:
            _report_install_failure(downloaded, output)
            return False
        installed = runner(
            [shell, str(installer)],
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode != 0:
            _report_install_failure(installed, output)
            return False
        _add_maestro_to_path()
        output(
            "Maestro installed. AppPilot will use it automatically; no PATH "
            "command is required."
        )
        return True


def _report_install_failure(
    result: subprocess.CompletedProcess,
    output: Callable[[str], None],
) -> None:
    """Display captured installer diagnostics when an installation fails."""
    detail = "\n".join(
        value.strip()
        for value in (getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        if value and value.strip()
    )
    if detail:
        output(detail)


def _add_maestro_to_path() -> None:
    """Make an installer-created Maestro binary available for this process."""
    maestro_bin = Path.home() / ".maestro" / "bin"
    if not maestro_bin.is_dir():
        return
    current = [item for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    if str(maestro_bin) not in current:
        os.environ["PATH"] = os.pathsep.join([str(maestro_bin), *current])
