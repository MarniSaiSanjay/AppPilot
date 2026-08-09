"""Build the officemobile Android APK from the omr enlistment.

Encapsulates the omrdroid build recipe so the deeplink suite can produce a
fresh local APK and install it via adb instead of tapping the Play Store.

Recipe (per owiki "Android innerloop on mac (omrdroid)"):
  1. If the enlistment has local changes, commit them as "temp - reset later".
  2. Check out the LKG branch and pull.
  3. Run `omrdroid build` (imports -> local.properties -> ./gradlew assembleDebug).
  4. Return the built APK path.

The build runs under zsh with init.sh sourced (bash is unsupported).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

ENLISTMENT_ROOT = Path("/Volumes/Office/omr1")
SRC_ROOT = ENLISTMENT_ROOT / "src"
JAVAKOTLIN_DIR = SRC_ROOT / "officemobile/android/JavaKotlin"
LKG_BRANCH = "lkg/main/android"

APK_PATH = (
    ENLISTMENT_ROOT
    / "Target/droidarm64/debug/officemobile/x-none/apk/debug/officemobile.apk"
)

JAVA_HOME = "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home"
NUGET_ROOT = "/Volumes/Office/NugetCache"

TEMP_COMMIT_MESSAGE = "temp - reset later"


class BuildError(RuntimeError):
    """Raised when the officemobile build cannot be completed."""


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(SRC_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _has_local_changes() -> bool:
    return bool(_git("status", "--porcelain"))


def _prepare_branch() -> None:
    """Stash-free reset: commit stray changes, then move to the LKG branch."""
    if _has_local_changes():
        _git("add", "-A")
        _git("commit", "-m", TEMP_COMMIT_MESSAGE)
    _git("checkout", LKG_BRANCH)
    _git("pull", "--ff-only")


def _run_omrdroid_build() -> str:
    prelude = "; ".join(
        [
            f"export JAVA_HOME={JAVA_HOME}",
            f"export NUGETMACHINEINSTALLROOT={NUGET_ROOT}",
            f"cd {SRC_ROOT}",
            "source ./init.sh >/dev/null 2>&1",
            f"cd {JAVAKOTLIN_DIR}",
            "omrdroid build",
        ]
    )
    # omrdroid exits 0 even when a step fails (failures downgraded to warnings),
    # so its return code can't be trusted. Capture and return output; APK
    # freshness is the authoritative check.
    result = subprocess.run(
        ["zsh", "-c", prelude], capture_output=True, text=True
    )
    output = f"{result.stdout or ''}{result.stderr or ''}"
    print(output, end="")
    return output


# Markers that mean a step actually failed even though omrdroid returns 0.
_FAILURE_MARKERS = (
    "BUILD FAILED",
    "assembleDebug for 'officemobile' failed",
    "import setup failed",
)


def build_apk() -> Path:
    """Build the officemobile APK and return its path.

    Raises ``BuildError`` unless a genuinely fresh APK is produced. Since
    omrdroid's exit code is unreliable, success requires no failure markers in
    the output AND an APK mtime newer than the build start (guards against a
    stale reuse)."""
    if not SRC_ROOT.exists():
        raise BuildError(f"Enlistment not found at {SRC_ROOT}")
    print("[BUILD] preparing enlistment (checkout LKG branch, pull)")
    _prepare_branch()
    print("[BUILD] compiling APK via omrdroid (this can take several minutes)")
    started = time.time()
    output = _run_omrdroid_build()

    print("[BUILD] verifying built APK")
    failures = [marker for marker in _FAILURE_MARKERS if marker in output]
    if failures:
        raise BuildError(
            "omrdroid reported build/step failure(s): "
            + "; ".join(sorted(set(failures)))
        )
    if not APK_PATH.exists():
        raise BuildError(f"Build produced no APK at {APK_PATH}")
    if APK_PATH.stat().st_mtime < started:
        raise BuildError(
            f"APK at {APK_PATH} was not rebuilt (stale); the build did not "
            "produce a fresh artifact"
        )
    return APK_PATH


def main() -> int:
    try:
        apk = build_apk()
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"[BUILD] FAILED: {exc}")
        return 1
    print(f"[BUILD] APK ready: {apk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
