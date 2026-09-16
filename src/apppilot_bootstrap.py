"""Stdlib-only first-run launcher for AppPilot setup."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


REPOSITORY = Path(__file__).resolve().parents[1]
_DEPENDENCY_CHECK = (
    "import azure.identity; import azure.keyvault.secrets"
)
_PYTHON_VERSION_CHECK = (
    "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)"
)


def _venv_python(repository: Path) -> Path:
    if os.name == "nt":
        return repository / ".venv" / "Scripts" / "python.exe"
    return repository / ".venv" / "bin" / "python"


def ensure_virtual_environment(
    *,
    repository: Path = REPOSITORY,
    executable: str = sys.executable,
    interactive: bool | None = None,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path | None:
    """Create AppPilot's venv and install dependencies after confirmation."""
    virtualenv = _venv_python(repository)
    supported = virtualenv.is_file() and _supported_python(virtualenv, runner)
    if supported and _dependencies_ready(virtualenv, runner):
        return virtualenv

    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    if not is_interactive:
        output(
            "AppPilot dependencies are not installed. Run /setup in an "
            "interactive terminal to create .venv."
        )
        return None
    message = (
        "AppPilot needs to recreate an unsupported .venv and install its Python "
        "dependencies. Continue? [y/N]: "
        if virtualenv.is_file() and not supported
        else "AppPilot needs to install its Python dependencies. Continue? [y/N]: "
    )
    answer = input_fn(message).strip().lower()
    if answer not in {"y", "yes"}:
        output("Python environment setup skipped.")
        return None

    if virtualenv.is_file() and not supported:
        try:
            shutil.rmtree(virtualenv.parent)
        except OSError:
            output("Could not remove AppPilot's unsupported Python environment.")
            return None
    if not virtualenv.is_file():
        created = runner(
            [executable, "-m", "venv", str(repository / ".venv")],
            check=False,
        )
        if created.returncode != 0:
            output("Could not create AppPilot's Python environment.")
            return None
    installed = runner(
        [str(virtualenv), "-m", "pip", "install", "-e", str(repository)],
        check=False,
    )
    if installed.returncode != 0:
        output("Could not install AppPilot's Python dependencies.")
        return None
    return virtualenv


def _dependencies_ready(
    virtualenv: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> bool:
    """Return whether a prior venv setup installed AppPilot runtime dependencies."""
    result = runner(
        [str(virtualenv), "-c", _DEPENDENCY_CHECK],
        check=False,
    )
    return result.returncode == 0


def _supported_python(
    virtualenv: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> bool:
    """Return whether a venv uses AppPilot's supported Python range."""
    result = runner(
        [str(virtualenv), "-c", _PYTHON_VERSION_CHECK],
        check=False,
    )
    return result.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    """Prepare the venv before importing AppPilot's Azure dependencies."""
    virtualenv = ensure_virtual_environment()
    if virtualenv is None:
        return 1
    result = subprocess.run(
        [str(virtualenv), str(REPOSITORY / "src" / "apppilot_setup.py"), *(argv or ())],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
