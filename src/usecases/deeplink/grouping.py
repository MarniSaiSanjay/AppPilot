"""Ordered License grouping for installed deeplink test cases."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

try:  # package-relative vs top-level (src on sys.path)
    from ...shared.credentials import normalize_profile_key
except ImportError:
    from shared.credentials import normalize_profile_key

from .deeplink_testcase_loader import DeeplinkTestCase


@dataclass(frozen=True)
class LicenseCaseGroup:
    """Installed cases sharing one normalized credential profile."""

    profile_key: str
    license_name: str
    cases: tuple[DeeplinkTestCase, ...]


def group_cases_by_license(
    cases: Sequence[DeeplinkTestCase],
) -> tuple[LicenseCaseGroup, ...]:
    """Group an installed-case subset while preserving first-seen order."""
    grouped: dict[str, list[DeeplinkTestCase]] = {}

    for case in cases:
        key = normalize_profile_key(case.license)
        grouped.setdefault(key, []).append(case)

    return tuple(
        LicenseCaseGroup(
            profile_key=key,
            license_name=" ".join(group_cases[0].license.split()),
            cases=tuple(group_cases),
        )
        for key, group_cases in grouped.items()
    )
