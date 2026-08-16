"""Deeplink per-case runner (use-case-specific deterministic orchestration).

Owns the deeplink attempt/retry recipe and bounded verification: launch the
EXACT deeplink, observe the Android UI, ask the judge whether it matches the
Expected Result, and retry deterministically on mismatch. Installed and
uninstalled scenarios differ only in how each attempt PREPARES the app; the
retry loop, verification, per-attempt recording and PASS/FAIL reporting are
identical. The model only judges - it never decides how to install, launch or
retry. Composes the shared login/installer/warm-up nodes.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

try:  # package-relative (python -m src.usecases.deeplink.runner) vs top-level
    from ...apppilot.android import (
        AndroidOperationalError,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from ...apppilot import logtags
    from ...shared.installer import AppInstaller
    from ...shared.login import LoginCapability
    from ...shared.warmup import WarmUp
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.android import (
        AndroidOperationalError,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from apppilot import logtags
    from shared.installer import AppInstaller
    from shared.login import LoginCapability
    from shared.warmup import WarmUp

from .testcases import DeeplinkTestCase
from .verification import ExpectationJudge, ExpectationJudgeOperationalError
from .results import AttemptResult, SuiteReport, TestCaseResult

# Deterministic bounds for the deeplink suite (separate from the agent's
# action/stuck limits). A failed test is retried once (1 attempt + 1 retry).
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_WAIT_SECONDS = 2.0
# Settle time after a deeplink launch before the first verification observation.
# Injected via the sleep hook so tests can no-op it.
DEFAULT_SETTLE_SECONDS = 3.0
# Bounded verification polling: observe -> judge repeatedly, PASS on first match,
# mismatch only after the window elapses. Same for installed AND uninstalled.
# Each observe+judge (a11y dump + AI judge) takes a few seconds, so this window
# fits ~3 validations - enough for a slow-settling screen to appear and be caught
# by a cheap re-check, instead of falling through to an expensive full retry
# (which re-opens the deeplink and, when uninstalled, re-installs the app).
DEFAULT_VERIFY_TIMEOUT_SECONDS = 30.0
DEFAULT_VERIFY_POLL_INTERVAL_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# The runner (deterministic orchestration; AI only judges)
# --------------------------------------------------------------------------- #
class DeeplinkTestRunner:
    def __init__(
        self,
        observer: MaestroHierarchyObserver,
        executor: MaestroExecutor,
        judge: ExpectationJudge,
        warm_up: WarmUp | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        verify_timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        verify_poll_interval_seconds: float = DEFAULT_VERIFY_POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        login_flow: LoginCapability | None = None,
        installer: AppInstaller | None = None,
    ) -> None:
        self._observer = observer
        self._executor = executor
        self._judge = judge
        self._warm_up = warm_up
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._retry_wait_seconds = retry_wait_seconds
        self._settle_seconds = settle_seconds
        self._verify_timeout_seconds = max(0.0, verify_timeout_seconds)
        self._verify_poll_interval_seconds = max(0.0, verify_poll_interval_seconds)
        self._monotonic = monotonic
        self._login_flow = login_flow
        self._installer = installer

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        # Delegate the top-level lifecycle to the explicit orchestrator, which
        # composes this runner's per-case execution. Kept as a convenience entry
        # point so existing callers/tests that hold a runner still work. Imported
        # lazily to avoid an import cycle (the orchestrator imports the runner).
        from .orchestrator import DeeplinkSuiteOrchestrator

        return DeeplinkSuiteOrchestrator(self).run(cases)

    def ensure_logged_in(self) -> bool:
        # Login only if needed, via the shared login capability. Returns True iff
        # login succeeded (True when no login flow is configured).
        if self._login_flow is not None:
            return self._login_flow.ensure_ready()
        return True

    def run_warm_up(self) -> None:
        # The installed warm-up (launch -> wait -> stop, x3). Invoked once per
        # installed batch by the orchestrator - never per case, never on retry.
        if self._warm_up is not None:
            self._warm_up()

    def install_local_build(self) -> None:
        # Put the freshly built local APK on the device (adb install -r). Used by
        # the installed batch so every installed case runs against the local build.
        if self._installer is not None:
            self._installer.install_fresh()

    def ensure_clean_install_state(self) -> None:
        # One-time suite-startup cleanup: guarantee the app is uninstalled so
        # every run starts from a deterministic clean state. No-op if absent or
        # no installer is configured.
        if self._installer is None:
            return
        if self._installer.ensure_absent():
            logtags.trace("Removed existing app install", logtags.SUITE)
        else:
            logtags.trace("No existing app install to remove", logtags.SUITE)

    def open_installed_app(self) -> None:
        # Launch the already-installed build to the foreground so login observes
        # the APP, not the launcher home screen. The batch only installs the APK
        # (install_fresh); the uninstalled path launches via install_and_open.
        if self._installer is not None:
            self._installer.open()

    def run_installed_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run a single INSTALLED case (kill -> wait 2s -> reopen retry)."""
        return self._run_case(case)

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run a single UNINSTALLED first-open case (fresh state every attempt)."""
        return self._run_uninstalled_case(case)

    def _verify(self, case: DeeplinkTestCase, attempt: int):
        """Shared, bounded verification polling for a single attempt.

        Used IDENTICALLY by installed and uninstalled cases. After the deeplink
        has been executed and the app is ready to be observed, repeatedly
        observe -> judge until the expected result matches (PASS immediately) or
        the bounded verification window elapses (genuine mismatch -> caller
        retries). A deeplink destination may take several seconds to appear, so a
        first non-matching observation is NOT a failure. Uses a monotonic clock
        so the window can never be skewed by wall-clock jumps, and always makes
        at least one observe/judge call.
        """
        deadline = self._monotonic() + self._verify_timeout_seconds
        verdict = None
        while True:
            logtags.trace(
                f"{case.test_id} attempt "
                f"{attempt}/{self._max_attempts}: checking expected result",
                logtags.VERIFY,
            )
            observation = self._observer.observe()
            verdict = self._judge.evaluate(case.expected_result, observation)
            if verdict.matched:
                logtags.trace(
                    f"{case.test_id}: expected result matched", logtags.VERIFY
                )
                return verdict
            if self._monotonic() >= deadline:
                logtags.trace(
                    f"{case.test_id}: verification timeout reached", logtags.VERIFY
                )
                return verdict
            logtags.trace(
                f"{case.test_id}: expected result not reached; "
                f"waiting {self._verify_poll_interval_seconds:g}s",
                logtags.VERIFY,
            )
            self._sleep(self._verify_poll_interval_seconds)

    def _run_attempts(
        self,
        case: DeeplinkTestCase,
        label: str,
        prepare: Callable[[int], None],
        on_start: "Callable[[], None] | None" = None,
    ) -> TestCaseResult:
        """Generic attempt loop shared by every scenario.

        Scenarios differ ONLY in how each attempt PREPARES the app before
        verification (``prepare``); the retry loop, shared bounded verification,
        per-attempt result recording and PASS/FAIL reporting are identical. A
        preparation failure is a retryable failed attempt, never a crash, so the
        suite always continues.
        """
        result = TestCaseResult(case=case)
        logtags.trace(f"{case.test_id} starting", label)
        if on_start is not None:
            on_start()
        for attempt in range(1, self._max_attempts + 1):
            reset_recovery = getattr(
                self._observer, "reset_recovery_budget", None
            )
            if callable(reset_recovery):
                reset_recovery()
            logtags.trace(
                f"{case.test_id} attempt {attempt}/{self._max_attempts}", label
            )
            try:
                prepare(attempt)
            except RuntimeError as exc:
                logtags.trace(f"{case.test_id} attempt setup failed: {exc}", label)
                result.attempts.append(
                    AttemptResult(attempt=attempt, matched=False, reason=str(exc))
                )
                continue

            if self._settle_seconds:
                self._sleep(self._settle_seconds)

            logtags.trace(f"{case.test_id} verifying deeplink expected result", label)
            try:
                verdict = self._verify(case, attempt)
            except (
                AndroidOperationalError,
                ExpectationJudgeOperationalError,
            ) as exc:
                logtags.trace(
                    f"{case.test_id} verification operational failure: {exc}",
                    label,
                )
                result.attempts.append(
                    AttemptResult(
                        attempt=attempt,
                        matched=False,
                        reason=f"verification operational failure: {exc}",
                    )
                )
                continue
            result.attempts.append(
                AttemptResult(
                    attempt=attempt, matched=verdict.matched, reason=verdict.reason
                )
            )
            if verdict.matched:
                break
            logtags.trace(
                f"{case.test_id} attempt "
                f"{attempt}/{self._max_attempts}: MISMATCH",
                label,
            )
        logtags.trace(
            f"{case.test_id} deeplink test case result: "
            f"{'PASS' if result.passed else 'FAIL'}",
            label,
        )
        return result

    def _run_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """INSTALLED scenario: retry recipe is kill -> wait -> reopen same deeplink.

        Process-isolated: the app is always stopped when the case finishes (PASS,
        FAIL, or error) via one finally boundary, so the next case's deeplink
        launches a fresh process. Installed-only; uninstalled is untouched.
        """
        try:
            return self._run_attempts(
                case, logtags.INSTALLED, self._installed_prepare(case)
            )
        finally:
            logtags.trace(
                f"{case.test_id} stopping app (case cleanup)", logtags.INSTALLED
            )
            # Cleanup must never abort the suite nor mask the case result: an
            # operational stop_app failure is logged and swallowed.
            try:
                self._executor.stop_app()
            except AndroidOperationalError as exc:
                logtags.trace(
                    f"{case.test_id} cleanup stop_app failed (ignored): {exc}",
                    logtags.INSTALLED,
                )

    def _installed_prepare(self, case: DeeplinkTestCase) -> Callable[[int], None]:
        def prepare(attempt: int) -> None:
            if attempt > 1:  # retry recipe: kill -> wait -> reopen the same deeplink
                logtags.trace(f"{case.test_id} retry: stopping app", logtags.INSTALLED)
                self._executor.stop_app()
                logtags.trace(
                    f"{case.test_id} retry: "
                    f"waiting {self._retry_wait_seconds:g}s",
                    logtags.INSTALLED,
                )
                self._sleep(self._retry_wait_seconds)
                logtags.trace(
                    f"{case.test_id} retry: reopening same deeplink",
                    logtags.INSTALLED,
                )
            else:
                logtags.trace(f"{case.test_id} opening deeplink", logtags.INSTALLED)
            self._executor.open_link(case.deep_link)

        return prepare

    def _run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """UNINSTALLED first-open scenario: NO warm-up. Every attempt re-establishes
        genuine fresh state (uninstall -> deeplink -> install/open -> shared login),
        so a retry can never silently degrade into an installed run."""
        return self._run_attempts(
            case,
            logtags.UNINSTALLED,
            self._uninstalled_prepare(case),
            on_start=lambda: logtags.trace(
                f"{case.test_id} first-open flow - warm-up not applicable",
                logtags.UNINSTALLED,
            ),
        )

    def _uninstalled_prepare(self, case: DeeplinkTestCase) -> Callable[[int], None]:
        def prepare(attempt: int) -> None:
            if attempt > 1:  # every retry rebuilds genuine fresh state
                logtags.trace(
                    f"{case.test_id} retry: recreating fresh-install state",
                    logtags.UNINSTALLED,
                )
            if self._installer is not None:
                logtags.trace(
                    f"{case.test_id} ensuring app is uninstalled",
                    logtags.UNINSTALLED,
                )
                self._installer.ensure_absent()
                logtags.trace(f"{case.test_id} app is uninstalled", logtags.UNINSTALLED)
            # 1) The EXACT deeplink routes to the store window while absent.
            logtags.trace(f"{case.test_id} opening deeplink", logtags.UNINSTALLED)
            self._executor.open_link(case.deep_link)
            logtags.trace(
                f"{case.test_id} deeplink dispatched while app "
                "absent; deferred handoff pending",
                logtags.UNINSTALLED,
            )
            if self._installer is not None:
                # 2) Install the local build via adb, then 3) open it by tapping
                # the store's Open button via Maestro (NOT re-firing the
                # deeplink). "app opened" is only emitted after the app is
                # confirmed foreground.
                logtags.trace(
                    f"{case.test_id} installing local build and opening via store button",
                    logtags.INSTALL,
                )
                self._installer.install_and_open(via_store_button=True)
                logtags.trace(f"{case.test_id} app opened", logtags.INSTALL)
            if self._login_flow is not None:  # SAME shared login as the installed path
                logtags.trace(f"{case.test_id} ensuring login", logtags.UNINSTALLED)
                # On login failure, raise into the per-attempt setup-failure path
                # (failed attempt -> skip _verify() -> retry fresh / else FAIL)
                # instead of reporting ready.
                if not self._login_flow.ensure_ready():
                    logtags.trace(f"{case.test_id} login failed", logtags.UNINSTALLED)
                    logtags.trace(
                        f"{case.test_id} skipping deeplink verification",
                        logtags.UNINSTALLED,
                    )
                    raise RuntimeError("login preparation failed")
                logtags.trace(f"{case.test_id} login ready", logtags.UNINSTALLED)
                logtags.trace(
                    f"{case.test_id} handing current UI to deeplink verification",
                    logtags.UNINSTALLED,
                )

        return prepare
