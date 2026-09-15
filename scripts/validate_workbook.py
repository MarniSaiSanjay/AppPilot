#!/usr/bin/env python3
"""Validate the tracked deeplink workbook and its release invariants."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from usecases.deeplink.deeplink_testcase_loader import load_deeplink_cases

WORKBOOK = REPO_ROOT / "testcases/deeplinks/DeeplinkTestCases.xlsx"
EXPECTED_CASE_IDS = tuple(f"TC{number:03d}" for number in range(1, 49))
EXPECTED_INSTALLATION_SPLIT = Counter({True: 24, False: 24})
EXPECTED_HOST_SPLIT = Counter(
    {
        "unifiedlink.svc.cloud.microsoft": 40,
        "m365.cloud.microsoft": 8,
    }
)


def validate() -> None:
    cases = load_deeplink_cases(WORKBOOK)
    case_ids = tuple(case.test_id for case in cases)
    if case_ids != EXPECTED_CASE_IDS:
        raise ValueError(
            "tracked workbook must contain exactly TC001-TC048 in order"
        )
    installation_split = Counter(case.installed for case in cases)
    if installation_split != EXPECTED_INSTALLATION_SPLIT:
        raise ValueError(
            "tracked workbook must contain 24 installed and 24 uninstalled cases"
        )
    host_split = Counter(urlsplit(case.deep_link).hostname for case in cases)
    if host_split != EXPECTED_HOST_SPLIT:
        raise ValueError(
            "tracked workbook host distribution changed unexpectedly: "
            f"{dict(host_split)}"
        )


def main() -> int:
    try:
        validate()
    except (OSError, ValueError) as error:
        print(f"workbook validation failed: {error}", file=sys.stderr)
        return 1
    print("workbook validation passed: 48 cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
