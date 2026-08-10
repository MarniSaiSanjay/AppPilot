"""Build-toolchain node.

One responsibility: confirm the two hard tools the APK build shells out to are
present BEFORE the suite starts a multi-minute build that would otherwise fail
deep inside ``[BUILD]``:

  * JDK 17 - the build exports ``JAVA_HOME`` to a fixed Temurin 17 location
    (``officemobile_build.JAVA_HOME``) and runs Gradle under it; Maestro also
    needs a JDK. We verify that ``JAVA_HOME/bin/java`` actually exists.
  * ``omrdroid`` - the CLI that drives the build recipe (imports ->
    local.properties -> ``./gradlew assembleDebug``). We verify it is on PATH.

Read-only, deterministic, never raises. The ``JAVA_HOME`` path and the PATH
lookup are injectable so tests need no real toolchain.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

from .. import officemobile_build
from .results import CheckResult

# Resolve "is this command on PATH?" - injectable for tests.
Which = Callable[[str], Optional[str]]

_JDK_INSTALL_HINT = (
    "install Temurin 17 (e.g. 'brew install --cask temurin@17') so it lives at "
    "that path, or update officemobile_build.JAVA_HOME to your JDK 17 home"
)
_OMRDROID_HINT = (
    "set up the omr enlistment tooling so the 'omrdroid' CLI is on PATH "
    "(per the 'Android innerloop on mac (omrdroid)' owiki)"
)


def check_build_tools(
    java_home: Optional[str] = None,
    which: Which = shutil.which,
) -> CheckResult:
    """Return whether JDK 17 and the ``omrdroid`` CLI are both available."""
    home = java_home if java_home is not None else officemobile_build.JAVA_HOME
    java_binary = Path(home) / "bin" / "java"
    if not java_binary.is_file():
        return CheckResult(
            False,
            f"JDK 17 not found at {java_binary} - the APK build needs it; "
            f"{_JDK_INSTALL_HINT}.",
        )

    if which("omrdroid") is None:
        return CheckResult(
            False,
            "'omrdroid' is not on PATH - the APK build needs it; "
            f"{_OMRDROID_HINT}.",
        )

    return CheckResult(True, "Build tools ready (JDK 17 + omrdroid).")
