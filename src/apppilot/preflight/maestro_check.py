"""Maestro availability node.

One responsibility: confirm the Maestro CLI is installed and runnable before the
suite starts. The framework drives the app entirely through the ``maestro``
binary (hierarchy observation + tap/text flows), so without it every action
fails mid-run with a cryptic error. This checks up front and returns a
structured, deterministic verdict. Read-only (``maestro --version``), never
raises, and the command runner is injectable for testing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]

_VERSION_TIMEOUT_SECONDS = 20
_INSTALL_URL = "https://maestro.mobile.dev"


@dataclass(frozen=True)
class ToolCheckResult:
    """Whether an external tool is usable, plus an actionable message."""

    ok: bool
    message: str
    version: Optional[str] = None


def _default_runner(args: Sequence[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        timeout=_VERSION_TIMEOUT_SECONDS,
    )


def check_maestro_ready(runner: CommandRunner = _default_runner) -> ToolCheckResult:
    """Return whether the Maestro CLI is installed and runnable."""
    try:
        result = runner(["maestro", "--version"])
    except FileNotFoundError:
        return ToolCheckResult(
            False,
            "Maestro is not installed or not on PATH - install it with "
            f'\'curl -Ls "{_INSTALL_URL}" | bash\' and reopen your shell.',
        )
    except (subprocess.SubprocessError, OSError) as error:
        return ToolCheckResult(False, f"could not run maestro: {error}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return ToolCheckResult(
            False, f"maestro --version failed: {detail or 'unknown error'}"
        )

    version = (result.stdout or "").strip().splitlines()
    version_str = version[0].strip() if version else None
    return ToolCheckResult(
        True, f"Maestro {version_str or 'ready'}.", version=version_str
    )
