#!/usr/bin/env python3
"""Run the local source-level PR checks, excluding the Docker build."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=REPO_ROOT, check=True)


def main() -> int:
    python = sys.executable
    commands = (
        (python, "-m", "compileall", "-q", "src", "tests", "scripts", "email_service"),
        (python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"),
        (python, "-m", "pyflakes", "src", "tests", "scripts", "email_service"),
        (python, "scripts/validate_workbook.py"),
        (python, "scripts/scan_secrets.py"),
        (
            python,
            "-m",
            "pip_audit",
            "-r",
            "email_service/requirements.txt",
            "--progress-spinner",
            "off",
        ),
        ("git", "diff", "--check", "HEAD"),
    )
    try:
        for command in commands:
            run(*command)
    except subprocess.CalledProcessError as error:
        return error.returncode
    print("all local PR source checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
