"""Deeplink result models and report formatting (use-case-specific)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .deeplink_testcase_loader import DeeplinkTestCase


@dataclass(frozen=True)
class AttemptResult:
    attempt: int
    matched: bool
    reason: str


@dataclass
class TestCaseResult:
    case: DeeplinkTestCase
    attempts: list[AttemptResult] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            any(attempt.matched for attempt in self.attempts)
            and not self.cleanup_errors
        )

    @property
    def deeplink_matched(self) -> bool:
        return any(attempt.matched for attempt in self.attempts)

    @property
    def cleanup_status(self) -> str:
        return "failed" if self.cleanup_errors else "completed"

    def record_cleanup_failure(self, reason: str) -> None:
        if reason not in self.cleanup_errors:
            self.cleanup_errors.append(reason)

    @property
    def passing_attempt(self) -> int | None:
        for attempt in self.attempts:
            if attempt.matched:
                return attempt.attempt
        return None


@dataclass
class SuiteReport:
    results: list[TestCaseResult] = field(default_factory=list)
    suite_name: str = "Test"
    cleanup_errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def format(self) -> str:
        lines = [
            f"{self.suite_name.upper()} TEST REPORT",
            "(Login is a precondition, reported inline per run as [LOGIN RESULT]. "
            "The PASS/FAIL below is the deeplink verification result, which is the "
            "overall test-case result.)",
            "",
        ]
        for result in self.results:
            status = "PASS" if result.passed else "FAIL"
            deeplink_status = "PASS" if result.deeplink_matched else "FAIL"
            attempt_no = result.passing_attempt or len(result.attempts)
            lines.append(
                f"{result.case.test_id}  {status}  Attempt {attempt_no}"
            )
            lines.append(f"    {self.suite_name} test: {deeplink_status}")
            lines.append(f"    Overall test case: {status}")
            lines.append(f"    Expected: {result.case.expected_result}")
            for attempt in result.attempts:
                mark = "match" if attempt.matched else "mismatch"
                lines.append(
                    f"    Attempt {attempt.attempt}: {mark} - {attempt.reason}"
                )
            lines.append(f"    Cleanup: {result.cleanup_status}")
            for error in result.cleanup_errors:
                lines.append(f"      Cleanup failure: {error}")
            lines.append("")
        if self.cleanup_errors:
            lines.append("Suite cleanup failures:")
            for error in self.cleanup_errors:
                lines.append(f"  - {error}")
            lines.append("")
        lines.append(f"Total test cases: {self.total}")
        lines.append(f"{self.suite_name} test cases passed: {self.passed}")
        lines.append(f"{self.suite_name} test cases failed: {self.failed}")
        return "\n".join(lines)

    def to_email_report(self):
        """Map this deeplink report onto the generic email view model."""
        from apppilot.email_render import (
            Cell,
            DetailAttempt,
            DetailCard,
            EmailReport,
        )

        ordered = sorted(
            self.results, key=lambda r: _testid_sort_key(r.case.test_id)
        )
        installed = [r for r in self.results if r.case.installed]
        uninstalled = [r for r in self.results if not r.case.installed]

        rows = []
        details = []
        for index, result in enumerate(ordered, start=1):
            attempt_no = result.passing_attempt or len(result.attempts)
            rows.append([
                Cell(text=str(index)),
                Cell(text=result.case.test_id, nowrap=True),
                Cell(text=result.case.user_type or "-"),
                Cell(text="yes" if result.case.installed else "no"),
                Cell(text=str(attempt_no)),
                Cell(text=result.case.expected_result, muted=True),
                Cell(status=result.passed, center=True),
            ])
            details.append(DetailCard(
                title=result.case.test_id,
                passed=result.passed,
                badge=f"Attempt {attempt_no}",
                meta_label="Expected",
                meta_value=result.case.expected_result,
                attempts=[
                    DetailAttempt(
                        label=f"Attempt {attempt.attempt}",
                        ok=attempt.matched,
                        text=attempt.reason,
                    )
                    for attempt in result.attempts
                ],
            ))

        return EmailReport(
            suite_name=self.suite_name,
            total=self.total,
            passed=self.passed,
            failed=self.failed,
            summary_segments=[
                _segment("Installed", installed),
                _segment("Uninstalled", uninstalled),
            ],
            columns=_EMAIL_COLUMNS,
            rows=rows,
            details=details,
            body_text=self.format(),
        )


_TESTID_CHUNK_RE = re.compile(r"(\d+)")

_EMAIL_COLUMNS = (
    "S.No", "Test ID", "User", "Installed", "Attempt", "Expected", "Result",
)


def _testid_sort_key(test_id: str):
    """Natural sort key for a Test ID."""
    chunks = _TESTID_CHUNK_RE.split(str(test_id))
    return [
        (1, int(chunk)) if chunk.isdigit() else (0, chunk.lower())
        for chunk in chunks
        if chunk != ""
    ]


def _segment(label: str, group: list) -> str:
    """One summary segment, e.g. ``Installed: 3 (2 passed, 1 failed)``."""
    passed = sum(1 for r in group if r.passed)
    return f"{label}: {len(group)} ({passed} passed, {len(group) - passed} failed)"


def _login_failed_result(case: DeeplinkTestCase) -> TestCaseResult:
    """FAIL result for a case whose login failed."""
    return TestCaseResult(
        case=case,
        attempts=[
            AttemptResult(
                attempt=1,
                matched=False,
                reason="login preparation failed; deeplink verification skipped",
            )
        ],
    )


def _batch_setup_failed_result(
    case: DeeplinkTestCase, reason: str
) -> TestCaseResult:
    """FAIL result for an installed-batch setup failure."""
    return TestCaseResult(
        case=case,
        attempts=[
            AttemptResult(
                attempt=1,
                matched=False,
                reason=(
                    f"installed batch setup failed: {reason}; "
                    "deeplink verification skipped"
                ),
            )
        ],
    )


def _startup_failed_result(
    case: DeeplinkTestCase, reason: str
) -> TestCaseResult:
    """FAIL result for a case that never ran."""
    return TestCaseResult(
        case=case,
        attempts=[
            AttemptResult(
                attempt=1,
                matched=False,
                reason=(
                    f"suite startup clean-install precondition failed: {reason}; "
                    "deeplink verification skipped"
                ),
            )
        ],
    )
