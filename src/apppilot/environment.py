"""Read-only environment probes for the AppPilot setup/readiness command.

Pure, side-effect-free system inspection: locate an executable on PATH and, when
present, safely ask it for its version. Nothing here installs, configures, or
mutates anything, and no probe ever raises to the caller - a missing tool, a
non-zero exit, a timeout, or an OS error all resolve to a structured result.

Dependency-injected (``which`` / ``runner``) so the whole layer is testable
without a real ``adb`` / ``maestro`` / ``java`` install. Stdlib-only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

# Bounded so a wedged tool can never hang setup; a slow ``--version`` is treated
# as "installed, version unknown" rather than blocking.
_DEFAULT_TIMEOUT = 10.0

# Matches a dotted version token such as ``1.39.0`` or ``35.0.2``. The LAST match
# in the output is used: tools like ``adb`` print an internal protocol version
# first (e.g. ``1.0.41``) and the human-facing release (e.g. ``35.0.2``) later.
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")

Which = Callable[[str], Optional[str]]
Runner = Callable[..., "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class ExecutableProbe:
    """Result of probing a single executable: whether it is on PATH and, if so,
    the version string parsed from its version output (``None`` if it could not
    be determined - the tool is present but did not report a parseable version).
    """

    found: bool
    version: Optional[str] = None


def extract_version(text: Optional[str]) -> Optional[str]:
    """Return the last dotted version token in ``text``, or ``None``.

    Last (not first) so multi-line tool banners that lead with an internal
    protocol version still surface the human-facing release version.
    """
    matches = _VERSION_RE.findall(text or "")
    return matches[-1] if matches else None


def run(
    command: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    timeout: float = _DEFAULT_TIMEOUT,
) -> "tuple[bool, str]":
    """Run ``command`` safely and return ``(succeeded, combined_output)``.

    Argument-list only (``shell=False``), bounded timeout, and fully
    exception-safe: any OS/subprocess error or timeout yields ``(False, "")``.
    ``succeeded`` reflects a zero exit status; ``combined_output`` is stdout and
    stderr concatenated and stripped. Generic and reusable by any framework-layer
    caller that needs a bounded, exception-safe command invocation.
    """
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
    """Locate ``name`` on PATH and, if present, parse its version.

    ``found`` reflects PATH presence only. ``version`` is best-effort: if the
    version command errors, times out, or prints nothing parseable, ``found``
    stays True and ``version`` is ``None`` (the caller renders "installed,
    version unknown"). Never raises.
    """
    if not which(name):
        return ExecutableProbe(found=False, version=None)
    ok, output = run([name, *version_args], runner=runner, timeout=timeout)
    version = extract_version(output) if ok else None
    return ExecutableProbe(found=True, version=version)
