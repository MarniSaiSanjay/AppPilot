"""Deeplink result models and report formatting (use-case-specific)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .testcases import DeeplinkTestCase


# --------------------------------------------------------------------------- #
# Results and reporting (deterministic)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AttemptResult:
    attempt: int
    matched: bool
    reason: str


@dataclass
class TestCaseResult:
    case: DeeplinkTestCase
    attempts: list[AttemptResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return any(attempt.matched for attempt in self.attempts)

    @property
    def passing_attempt(self) -> int | None:
        for attempt in self.attempts:
            if attempt.matched:
                return attempt.attempt
        return None


@dataclass
class SuiteReport:
    results: list[TestCaseResult] = field(default_factory=list)

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
            "DEEPLINK TEST REPORT",
            "(Login is a precondition, reported inline per run as [LOGIN RESULT]. "
            "The PASS/FAIL below is the deeplink verification result, which is the "
            "overall test-case result.)",
            "",
        ]
        for result in self.results:
            status = "PASS" if result.passed else "FAIL"
            attempt_no = result.passing_attempt or len(result.attempts)
            lines.append(
                f"{result.case.test_id}  {status}  Attempt {attempt_no}"
            )
            lines.append(f"    Deeplink test: {status}")
            lines.append(f"    Overall test case: {status}")
            lines.append(f"    Expected: {result.case.expected_result}")
            for attempt in result.attempts:
                mark = "match" if attempt.matched else "mismatch"
                lines.append(
                    f"    Attempt {attempt.attempt}: {mark} - {attempt.reason}"
                )
            lines.append("")
        lines.append(f"Total test cases: {self.total}")
        lines.append(f"Deeplink test cases passed: {self.passed}")
        lines.append(f"Deeplink test cases failed: {self.failed}")
        return "\n".join(lines)


def _login_failed_result(case: DeeplinkTestCase) -> TestCaseResult:
    """FAIL result for a case whose login failed: one failed attempt, no deeplink
    verdict (``_verify()`` never ran). Used by the installed batch on login
    failure; the uninstalled flow reaches this via the per-attempt setup path."""
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
    """FAIL result for a case in an installed batch whose shared install/launch
    setup failed operationally: one failed attempt, no deeplink verdict. The
    suite still continues and the report/email are still produced."""
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
    """FAIL result for a case that never ran because the one-time suite-startup
    clean-install precondition failed operationally: one failed attempt, no
    deeplink verdict. The real reason is preserved (never masked, never a false
    PASS) and the suite still returns a report so final reporting/email happen."""
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
