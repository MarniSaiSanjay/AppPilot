#!/usr/bin/env python3
"""Validate that the tracked deeplink workbook can be loaded."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from usecases.deeplink.deeplink_testcase_loader import load_deeplink_cases

WORKBOOK = REPO_ROOT / "testcases/deeplinks/DeeplinkTestCases.xlsx"


def validate() -> int:
    cases = load_deeplink_cases(WORKBOOK)
    if not cases:
        raise ValueError("tracked workbook must contain at least one test case")
    return len(cases)


def main() -> int:
    try:
        validate()
    except (OSError, ValueError) as error:
        print(f"workbook validation failed: {error}", file=sys.stderr)
        return 1
    print("workbook validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
