"""Read-only environment probes for setup/readiness."""

from __future__ import annotations

import re
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, MutableMapping, Optional, Sequence

_DEFAULT_TIMEOUT = 10.0

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")

Which = Callable[[str], Optional[str]]
Runner = Callable[..., "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class ExecutableProbe:
    """Result of probing a single executable."""

    found: bool
    version: Optional[str] = None


def extract_version(text: Optional[str]) -> Optional[str]:
    """Return the last dotted version token in ``text``."""
    matches = _VERSION_RE.findall(text or "")
    return matches[-1] if matches else None


def run(
    command: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    timeout: float = _DEFAULT_TIMEOUT,
) -> "tuple[bool, str]":
    """Run ``command`` and return ``(succeeded, combined_output)``."""
    try:
        result = runner(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False, ""
    combined = ((getattr(result, "stdout", "") or "")
                + (getattr(result, "stderr", "") or "")).strip()
    ok = getattr(result, "returncode", 1) == 0
    return ok, combined


def probe_executable(
    name: str,
    version_args: Sequence[str],
    *,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ExecutableProbe:
    """Locate ``name`` on PATH and parse its version."""
    if not which(name):
        return ExecutableProbe(found=False, version=None)
    ok, output = run([name, *version_args], runner=runner, timeout=timeout)
    version = extract_version(output) if ok else None
    return ExecutableProbe(found=True, version=version)


def android_sdk_root(
    *,
    env: MutableMapping[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    """Return an existing Android SDK root from configuration or OS defaults."""
    source = os.environ if env is None else env
    configured = source.get("ANDROID_SDK_ROOT") or source.get("ANDROID_HOME")
    if configured:
        root = Path(configured).expanduser()
        return root if root.is_dir() else None

    defaults: list[Path] = []
    if local_app_data := source.get("LOCALAPPDATA"):
        windows_default = Path(local_app_data) / "Android" / "Sdk"
        return windows_default if windows_default.is_dir() else None
    resolved_home = Path.home() if home is None else home
    defaults.extend((
        resolved_home / "Library" / "Android" / "sdk",
        resolved_home / "Android" / "Sdk",
    ))
    return next((root for root in defaults if root.is_dir()), None)


def add_android_sdk_paths(
    *,
    env: MutableMapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[str, ...]:
    """Add existing Android SDK tool directories to PATH for this process."""
    source = os.environ if env is None else env
    root = android_sdk_root(env=source, home=home)
    if root is None:
        return ()

    candidates = [root / "platform-tools", root / "emulator"]
    candidates.extend(sorted(
        (root / "cmdline-tools").glob("*/bin"),
        reverse=True,
    ))
    candidates.extend(sorted(
        (root / "build-tools").iterdir() if (root / "build-tools").is_dir() else (),
        reverse=True,
    ))
    additions = [
        str(candidate)
        for candidate in candidates
        if candidate.is_dir()
    ]
    current = [item for item in source.get("PATH", "").split(os.pathsep) if item]
    new_entries = [entry for entry in additions if entry not in current]
    if new_entries:
        source["PATH"] = os.pathsep.join([*new_entries, *current])
    if "ANDROID_SDK_ROOT" not in source:
        source["ANDROID_SDK_ROOT"] = str(root)
    return tuple(new_entries)


def add_java_path(
    *,
    env: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Add a locally installed JDK ``bin`` directory to PATH for this process."""
    source = os.environ if env is None else env
    candidates: list[Path] = []
    if java_home := source.get("JAVA_HOME"):
        candidates.append(Path(java_home).expanduser() / "bin")

    program_files = (
        source.get("ProgramFiles"),
        source.get("PROGRAMFILES"),
        source.get("ProgramW6432"),
    )
    for directory in dict.fromkeys(path for path in program_files if path):
        root = Path(directory)
        for vendor in ("Eclipse Adoptium", "Java", "Microsoft", "Zulu"):
            vendor_root = root / vendor
            if vendor_root.is_dir():
                candidates.extend(sorted(vendor_root.glob("*/bin"), reverse=True))

    java_names = ("java.exe", "java")
    additions = [
        str(candidate)
        for candidate in candidates
        if candidate.is_dir() and any((candidate / name).is_file() for name in java_names)
    ]
    current = [item for item in source.get("PATH", "").split(os.pathsep) if item]
    new_entries = [entry for entry in additions if entry not in current]
    if new_entries:
        source["PATH"] = os.pathsep.join([*new_entries, *current])
    return tuple(new_entries)


def git_bash_path(
    *,
    env: MutableMapping[str, str] | None = None,
) -> str | None:
    """Return Git Bash from standard Windows install locations when available."""
    source = os.environ if env is None else env
    roots = (
        source.get("ProgramFiles"),
        source.get("PROGRAMFILES"),
        source.get("ProgramW6432"),
        (
            str(Path(source["LOCALAPPDATA"]) / "Programs")
            if source.get("LOCALAPPDATA")
            else None
        ),
    )
    for directory in dict.fromkeys(path for path in roots if path):
        bash = Path(directory) / "Git" / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    return None
