"""Deeplink suite orchestration."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Callable, Optional, Sequence, TYPE_CHECKING

try:
    from ...apppilot import logtags
except ImportError:
    from apppilot import logtags

from .deeplink_testcase_loader import DeeplinkTestCase
from .grouping import LicenseCaseGroup, group_cases_by_license
from .results import (
    SuiteReport,
    TestCaseResult,
    _batch_setup_failed_result,
    _login_failed_result,
    _startup_failed_result,
)

if TYPE_CHECKING:
    from .runner import DeeplinkTestRunner


class _SupportedLinkPreparationError(Exception):
    """Installed-batch supported-link configuration failed."""

class DeeplinkSuiteOrchestrator:
    """Runs the deeplink suite lifecycle."""

    SUITE_NAME = "Deeplink"

    def __init__(
        self,
        runner: "DeeplinkTestRunner",
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._runner = runner
        self._progress = progress
        self._total = 0

    def _emit(self, report: SuiteReport, result: TestCaseResult) -> None:
        """Append a completed result and print a live one-line progress update."""
        report.results.append(result)
        if self._progress is None:
            return
        status = "PASS" if result.passed else "FAIL"
        line = f"[{report.total}/{self._total}] {result.case.test_id}  {status}"
        if not result.passed:
            line += f"   ({self._progress_reason(result)})"
        self._progress(line)

    @staticmethod
    def _progress_reason(result: TestCaseResult) -> str:
        """Short, stable failure category for the live progress line."""
        reason = result.attempts[-1].reason if result.attempts else ""
        if "login preparation failed" in reason:
            return "login failed"
        if "installed batch setup failed" in reason:
            return "setup failed"
        if "startup clean-install precondition failed" in reason:
            return "startup failed"
        if result.cleanup_errors:
            return "cleanup failed"
        return "deeplink mismatch"

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        installed = [case for case in cases if case.installed]
        uninstalled = [case for case in cases if not case.installed]
        self._total = len(cases)

        logtags.trace("Starting deeplink test suite", logtags.SUITE)
        logtags.trace(f"Loaded {len(cases)} test cases", logtags.SUITE)
        logtags.trace(f"Installed cases: {len(installed)}", logtags.SUITE)
        logtags.trace(f"Uninstalled cases: {len(uninstalled)}", logtags.SUITE)

        report = SuiteReport(suite_name=self.SUITE_NAME)
        try:
            try:
                self._runner.ensure_clean_install_state()
            except RuntimeError as exc:
                logtags.trace(
                    f"startup clean-install precondition failed: {exc}; "
                    "skipping all test execution",
                    logtags.SUITE,
                )
                for case in cases:
                    self._emit(report, _startup_failed_result(case, str(exc)))
                logtags.trace("Aborted before test execution", logtags.SUITE)
                return report

            if installed:
                self.run_installed_batch(installed, report)
            for case in uninstalled:
                self._emit(report, self.run_uninstalled_case(case))
            self._restore_workbook_order(report, cases)
            logtags.trace("Completed", logtags.SUITE)
            return report
        finally:
            try:
                self._runner.finalize_suite(report)
            except Exception as error:
                reason = f"suite cleanup failed unexpectedly: {error}"
                report.cleanup_errors.append(reason)
                for result in report.results:
                    result.record_cleanup_failure(reason)

    def run_installed_batch(
        self, cases: Sequence[DeeplinkTestCase], report: SuiteReport
    ) -> None:
        logtags.trace("Starting", logtags.INSTALLED_BATCH)
        try:
            groups = group_cases_by_license(cases)
            logtags.trace("Installing local build", logtags.INSTALLED_BATCH)
            self._runner.install_local_build()
        except RuntimeError as exc:

            logtags.trace(f"installed batch setup failed: {exc}", logtags.INSTALLED_BATCH)
            for case in cases:
                logtags.trace(
                    f"{case.test_id} skipping deeplink verification",
                    logtags.INSTALLED,
                )
                self._emit(report, _batch_setup_failed_result(case, str(exc)))
            return
        supported_links_prepared = False
        for index, group in enumerate(groups):
            try:
                if self._run_installed_group(
                    group,
                    report,
                    prepare_supported_links=not supported_links_prepared,
                ):
                    supported_links_prepared = True
            except _SupportedLinkPreparationError as exc:
                logtags.trace(
                    f"supported-link preparation failed: {exc}",
                    logtags.INSTALLED_BATCH,
                )
                for remaining_group in groups[index:]:
                    for case in remaining_group.cases:
                        self._emit(
                            report,
                            _batch_setup_failed_result(case, str(exc)),
                        )
                return

    def _run_installed_group(
        self,
        group: LicenseCaseGroup,
        report: SuiteReport,
        *,
        prepare_supported_links: bool,
    ) -> bool:
        representative = group.cases[0]
        prepared_supported_links = False
        try:
            logtags.trace(
                "Launching app before account preparation",
                logtags.INSTALLED_BATCH,
            )
            self._runner.open_installed_app()
            logtags.trace("Ensuring login", logtags.INSTALLED_BATCH)
            if not self._ensure_installed_group_login(representative):
                for case in group.cases:
                    self._emit(report, _login_failed_result(case))
                return False
            if prepare_supported_links:
                try:
                    self._runner.prepare_supported_links()
                except (RuntimeError, ValueError) as exc:
                    raise _SupportedLinkPreparationError(str(exc)) from exc
                prepared_supported_links = True
            preparation = self._runner.prepare_account(representative)
            if not preparation.succeeded:
                for case in group.cases:
                    self._emit(
                        report,
                        _batch_setup_failed_result(case, preparation.reason),
                    )
                return prepared_supported_links
            self._runner.run_warm_up()
            self._runner.open_installed_app()
            if not self._ensure_installed_group_login(representative):
                for case in group.cases:
                    self._emit(report, _login_failed_result(case))
                return prepared_supported_links
        except RuntimeError as exc:
            logtags.trace(
                f"License group setup failed: {exc}",
                logtags.INSTALLED_BATCH,
            )
            for case in group.cases:
                self._emit(report, _batch_setup_failed_result(case, str(exc)))
            return prepared_supported_links
        for case in group.cases:
            self._emit(report, self._runner.run_installed_case(case))
        return prepared_supported_links

    def _ensure_installed_group_login(self, case: DeeplinkTestCase) -> bool:
        if self._runner.ensure_logged_in(case):
            return True
        logtags.trace(
            "Login preparation failed; relaunching app and retrying once",
            logtags.INSTALLED_BATCH,
        )
        self._runner.open_installed_app()
        return self._runner.ensure_logged_in(case)

    @staticmethod
    def _restore_workbook_order(
        report: SuiteReport,
        cases: Sequence[DeeplinkTestCase],
    ) -> None:
        results_by_case = defaultdict(deque)
        for result in report.results:
            results_by_case[id(result.case)].append(result)
        ordered = []
        for case in cases:
            case_results = results_by_case[id(case)]
            if not case_results:
                raise RuntimeError(
                    "suite execution did not produce exactly one result per case"
                )
            ordered.append(case_results.popleft())
        if any(results_by_case.values()):
            raise RuntimeError(
                "suite execution did not produce exactly one result per case"
            )
        report.results = ordered

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        return self._runner.run_uninstalled_case(case)
