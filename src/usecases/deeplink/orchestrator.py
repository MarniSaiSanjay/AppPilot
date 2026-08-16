"""Deeplink suite orchestrator (use-case-specific top-level lifecycle).

Makes the deeplink suite lifecycle explicit: split cases by the deterministic
INSTALLED value, prepare the installed batch (login-if-needed + one-time
warm-up), and drive each case - while COMPOSING the DeeplinkTestRunner for
individual case execution, semantic judging, retry and reporting.
"""

from __future__ import annotations

from typing import Sequence, TYPE_CHECKING

try:  # package-relative (python -m src.usecases.deeplink.orchestrator) vs top-level
    from ...apppilot import logtags
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot import logtags

from .testcases import DeeplinkTestCase
from .results import (
    SuiteReport,
    TestCaseResult,
    _batch_setup_failed_result,
    _login_failed_result,
    _startup_failed_result,
)

if TYPE_CHECKING:  # avoid an import cycle (runner.run imports the orchestrator)
    from .runner import DeeplinkTestRunner


# --------------------------------------------------------------------------- #
# Suite orchestrator (explicit top-level lifecycle; composes the runner)
# --------------------------------------------------------------------------- #
class DeeplinkSuiteOrchestrator:
    """Makes the deeplink suite lifecycle explicit and readable.

    It owns only the top-level flow - splitting cases by the deterministic
    INSTALLED value, preparing the installed batch (login-if-needed + one-time
    warm-up), and driving each case - while COMPOSING the existing
    DeeplinkTestRunner for individual case execution, semantic judging, retry
    behavior, and reporting. Nothing here duplicates the runner, the shared
    login capability, the judge, the app installer, or Maestro/Android
    behavior.

        run()
            -> INSTALLED batch: prepare_installed_batch() then run each case
            -> UNINSTALLED cases: run each first-open case (no warm-up)
            -> final SuiteReport
    """

    def __init__(self, runner: "DeeplinkTestRunner") -> None:
        self._runner = runner

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        # INSTALLED is deterministic (from Excel); installed cases run as one
        # batch (single login-if-needed + single warm-up), then each uninstalled
        # first-open case runs independently.
        installed = [case for case in cases if case.installed]
        uninstalled = [case for case in cases if not case.installed]

        logtags.trace("Starting deeplink test suite", logtags.SUITE)
        logtags.trace(f"Loaded {len(cases)} test cases", logtags.SUITE)
        logtags.trace(f"Installed cases: {len(installed)}", logtags.SUITE)
        logtags.trace(f"Uninstalled cases: {len(uninstalled)}", logtags.SUITE)

        # One-time clean state: uninstall the app once at suite startup so every
        # run begins deterministically. If this operational precondition fails
        # (e.g. device offline / adb uninstall failure) no case can run safely,
        # so record every case as a startup failure and return the report -
        # never abort. The real reason is preserved (no false PASS) and final
        # reporting/email still happen. Existing per-case semantics are unchanged.
        report = SuiteReport()
        try:
            self._runner.ensure_clean_install_state()
        except RuntimeError as exc:
            logtags.trace(
                f"startup clean-install precondition failed: {exc}; "
                "skipping all test execution",
                logtags.SUITE,
            )
            for case in cases:
                report.results.append(_startup_failed_result(case, str(exc)))
            logtags.trace("Aborted before test execution", logtags.SUITE)
            return report

        if installed:
            self.run_installed_batch(installed, report)
        for case in uninstalled:
            report.results.append(self.run_uninstalled_case(case))
        # Only reached when the startup precondition held and cases executed; a
        # setup failure is recorded above and returns early, so "Completed" is
        # never misleading.
        logtags.trace("Completed", logtags.SUITE)
        return report

    def prepare_installed_batch(self) -> bool:
        # Once per batch: install the local APK, launch it, log in, then warm up
        # (never per case / retry). Returns True iff login succeeded; on failure
        # skip warm-up and don't proceed to verification.
        logtags.trace("Installing local build", logtags.INSTALLED_BATCH)
        self._runner.install_local_build()
        logtags.trace("Launching app before login", logtags.INSTALLED_BATCH)
        self._runner.open_installed_app()
        logtags.trace("Ensuring login", logtags.INSTALLED_BATCH)
        if not self._runner.ensure_logged_in():
            logtags.trace("login failed", logtags.INSTALLED_BATCH)
            return False
        self._runner.run_warm_up()
        return True

    def run_installed_batch(
        self, cases: Sequence[DeeplinkTestCase], report: SuiteReport
    ) -> None:
        logtags.trace("Starting", logtags.INSTALLED_BATCH)
        try:
            prepared = self.prepare_installed_batch()
        except RuntimeError as exc:
            # Operational install/launch/setup failure (includes
            # AndroidOperationalError): record every case as a batch-setup failure
            # and continue the suite so unrelated cases still run and the final
            # report/email are still produced.
            logtags.trace(f"installed batch setup failed: {exc}", logtags.INSTALLED_BATCH)
            for case in cases:
                logtags.trace(
                    f"{case.test_id} skipping deeplink verification",
                    logtags.INSTALLED,
                )
                report.results.append(_batch_setup_failed_result(case, str(exc)))
            return
        if not prepared:
            # Batch login failed: record every case as a login-prep failure
            # (no _verify()), so each is FAIL like the uninstalled flow.
            for case in cases:
                logtags.trace(f"{case.test_id} login failed", logtags.INSTALLED)
                logtags.trace(
                    f"{case.test_id} skipping deeplink verification",
                    logtags.INSTALLED,
                )
                report.results.append(_login_failed_result(case))
            return
        for case in cases:
            report.results.append(self._runner.run_installed_case(case))

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        # First-open-after-install: NO warm-up. The runner re-establishes the
        # genuine fresh/uninstalled state on every attempt.
        return self._runner.run_uninstalled_case(case)
