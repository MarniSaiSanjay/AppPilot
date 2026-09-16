"""Read-only environment probes for setup/readiness."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

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
