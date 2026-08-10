"""Python-version check node.

One responsibility: confirm the interpreter running AppPilot is new enough. The
framework relies on 3.10+ syntax (e.g. ``X | Y`` unions, structural typing) and
is validated on 3.11+, so an older interpreter fails in confusing ways. This
checks up front and returns a deterministic verdict. Pure, never raises.
"""

from __future__ import annotations

import sys
from typing import Sequence

from .results import CheckResult

# Minimum interpreter AppPilot supports; keep in sync with the README.
MIN_PYTHON = (3, 11)


def check_python_version(
    version_info: Sequence[int] = sys.version_info,
) -> CheckResult:
    """Return whether the running Python is at least :data:`MIN_PYTHON`."""
    major, minor = version_info[0], version_info[1]
    required = ".".join(str(part) for part in MIN_PYTHON)
    running = f"{major}.{minor}"
    if (major, minor) < MIN_PYTHON:
        return CheckResult(
            False,
            f"Python {required}+ is required but this is {running} - "
            f"run AppPilot with a newer interpreter (e.g. 'python{required}').",
        )
    return CheckResult(True, f"Python {running}.")
