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
from urllib.parse import urlsplit

try:  # package-relative (python -m src.usecases.deeplink.runner) vs top-level
    from ...apppilot.android import (
        AndroidOperationalError,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from ...apppilot import logtags
    from ...apppilot.models import Action, ActionKind, UIElement, UIObservation
    from ...shared.account import (
        AccountPreparationKind,
        AccountPreparationResult,
        AccountSessionCapability,
    )
    from ...shared.credentials import CredentialConfigurationError
    from ...shared.credentials import CredentialProfile
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
    from apppilot.models import Action, ActionKind, UIElement, UIObservation
    from shared.account import (
        AccountPreparationKind,
        AccountPreparationResult,
        AccountSessionCapability,
    )
    from shared.credentials import CredentialConfigurationError
    from shared.credentials import CredentialProfile
    from shared.installer import AppInstaller
    from shared.login import LoginCapability
    from shared.warmup import WarmUp

from .deeplink_testcase_loader import DeeplinkTestCase
from .verification import ExpectationJudge, ExpectationJudgeOperationalError
from .results import AttemptResult, SuiteReport, TestCaseResult
from .supported_links import SUPPORTED_LINK_DOMAINS

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
_RESEARCHER_ADD_VERBS = {"add", "get", "install"}


class _ReplayRecoveryError(RuntimeError):
    """A restart-and-replay recovery failed before verification could resume."""


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
        login_flow_for_license: "Callable[[str], LoginCapability] | None" = None,
        account_session: AccountSessionCapability | None = None,
        profile_for_license: "Callable[[str], CredentialProfile] | None" = None,
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
        self._login_flow_for_license = login_flow_for_license
        self._account_session = account_session
        self._profile_for_license = profile_for_license
        self._installer = installer
        self._supported_links_prepared = False

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        # Delegate the top-level lifecycle to the explicit orchestrator, which
        # composes this runner's per-case execution. Kept as a convenience entry
        # point so existing callers/tests that hold a runner still work. Imported
        # lazily to avoid an import cycle (the orchestrator imports the runner).
        from .orchestrator import DeeplinkSuiteOrchestrator

        return DeeplinkSuiteOrchestrator(self).run(cases)

    def _login_flow_for_case(
        self, case: DeeplinkTestCase
    ) -> LoginCapability | None:
        if self._login_flow_for_license is not None:
            return self._login_flow_for_license(case.license)
        return self._login_flow

    def ensure_logged_in(self, case: DeeplinkTestCase | None = None) -> bool:
        # Login only if needed, via the shared login capability. Returns True iff
        # login succeeded (True when no login flow is configured).
        if self._login_flow_for_license is not None:
            if case is None:
                raise RuntimeError(
                    "A deeplink test case is required to select login credentials"
                )
            login_flow = self._login_flow_for_case(case)
        else:
            login_flow = self._login_flow
        if login_flow is not None:
            return login_flow.ensure_ready()
        return True

    def run_warm_up(self) -> None:
        # Installed stabilization (launch -> wait -> stop). Invoked once per
        # successfully prepared License group - never per case or retry.
        if self._warm_up is not None:
            self._warm_up()

    def prepare_supported_links(self) -> None:
        """Prepare installed-app link routing after login."""
        logtags.trace(
            f"Enabling {len(SUPPORTED_LINK_DOMAINS)} supported-link domain(s)",
            logtags.INSTALLED_BATCH,
        )
        self._executor.enable_supported_links(SUPPORTED_LINK_DOMAINS)
        self._supported_links_prepared = True

    def prepare_account(
        self, case: DeeplinkTestCase
    ) -> AccountPreparationResult:
        """Ensure the resolved License profile is the active app account."""
        if self._account_session is None:
            return AccountPreparationResult.ready(
                AccountPreparationKind.ALREADY_ACTIVE
            )
        if self._profile_for_license is None:
            raise RuntimeError(
                "Account preparation requires resolved credential profiles"
            )
        profile = self._profile_for_license(case.license)
        return self._account_session.ensure_active(profile)

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
        """Run an INSTALLED case with ready-state restoration on retry."""
        try:
            return self._run_attempts(
                case, logtags.INSTALLED, self._installed_prepare(case)
            )
        finally:
            logtags.trace(
                f"{case.test_id} stopping app (case cleanup)", logtags.INSTALLED
            )
            # Cleanup must never abort the suite nor mask the case result.
            try:
                self._executor.stop_app()
            except AndroidOperationalError as exc:
                logtags.trace(
                    f"{case.test_id} cleanup stop_app failed (ignored): {exc}",
                    logtags.INSTALLED,
                )

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run an UNINSTALLED first-open case with phase-aware retries."""
        prepare, invalidate_recovery = self._uninstalled_prepare(case)
        return self._run_attempts(
            case,
            logtags.UNINSTALLED,
            prepare,
            on_start=lambda: logtags.trace(
                f"{case.test_id} first-open flow - warm-up not applicable",
                logtags.UNINSTALLED,
            ),
            on_replay_recovery_failure=invalidate_recovery,
        )

    def _verify(self, case: DeeplinkTestCase, attempt: int):
        """Shared, bounded verification polling for a single attempt.

        Used IDENTICALLY by installed and uninstalled cases. After the deeplink
        has been executed and the app is ready to be observed, add Researcher
        when its own screen explicitly offers that action, then repeatedly
        observe -> judge the resulting destination until the expected result
        matches (PASS immediately) or the bounded verification window elapses
        (genuine mismatch -> caller retries). Unknown screens go directly to the
        judge. Uses a monotonic clock so the window can never be skewed by
        wall-clock jumps, and always makes at least one observe/judge call.
        """
        deadline = self._monotonic() + self._verify_timeout_seconds
        researcher_add_attempted = False
        transient_recovery_used = False
        while True:
            logtags.trace(
                f"{case.test_id} attempt "
                f"{attempt}/{self._max_attempts}: checking expected result",
                logtags.VERIFY,
            )
            try:
                observation = self._observer.observe()
                if not researcher_add_attempted and (
                    add_researcher := self._find_researcher_add_control(observation)
                ) is not None:
                    logtags.trace(
                        f"{case.test_id}: adding Researcher agent",
                        logtags.VERIFY,
                    )
                    self._executor.execute(
                        Action(ActionKind.TAP, target_id=add_researcher.element_id),
                        observation,
                    )
                    researcher_add_attempted = True
                    deadline = self._monotonic() + self._verify_timeout_seconds
                    self._sleep(self._verify_poll_interval_seconds)
                    continue
            except AndroidOperationalError as exc:
                if (
                    transient_recovery_used
                    or not self._can_restart_and_replay(case)
                ):
                    raise
                self._recover_transient_attempt(case, str(exc))
                transient_recovery_used = True
                researcher_add_attempted = False
                deadline = self._monotonic() + self._verify_timeout_seconds
                continue
            verdict = self._judge.evaluate(case.expected_result, observation)
            if verdict.matched:
                logtags.trace(
                    f"{case.test_id}: expected result matched", logtags.VERIFY
                )
                return verdict
            if self._monotonic() >= deadline:
                if (
                    not transient_recovery_used
                    and self._can_restart_and_replay(case)
                    and self._is_incomplete_destination(observation)
                ):
                    self._recover_transient_attempt(
                        case,
                        "expected destination shell remained incomplete",
                    )
                    transient_recovery_used = True
                    researcher_add_attempted = False
                    deadline = self._monotonic() + self._verify_timeout_seconds
                    continue
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

    def _recover_transient_attempt(
        self,
        case: DeeplinkTestCase,
        reason: str,
    ) -> None:
        """Restart once and replay the exact deeplink after a transient failure."""
        logtags.trace(
            f"{case.test_id}: transient recovery triggered: {reason}",
            logtags.VERIFY,
        )
        try:
            self._restart_ready_and_replay(case)
        except CredentialConfigurationError:
            raise
        except RuntimeError as exc:
            raise _ReplayRecoveryError(str(exc)) from exc
        reset_recovery = getattr(self._observer, "reset_recovery_budget", None)
        if callable(reset_recovery):
            reset_recovery()
        if self._settle_seconds:
            self._sleep(self._settle_seconds)
        logtags.trace(
            f"{case.test_id}: transient recovery replayed exact deeplink",
            logtags.VERIFY,
        )

    def _restart_ready_and_replay(self, case: DeeplinkTestCase) -> None:
        """Restart the installed app, restore prerequisites, and replay its link."""
        self._executor.stop_app()
        if self._retry_wait_seconds:
            self._sleep(self._retry_wait_seconds)
        if self._installer is not None:
            self._installer.open()
        else:
            self._executor.launch_app()
        self._ensure_login_after_restart(case)
        self._prepare_supported_link_if_needed(case.deep_link)
        self._executor.open_link(case.deep_link)

    def _ensure_login_after_restart(self, case: DeeplinkTestCase) -> None:
        """Confirm readiness after restart and act only when login is required."""
        login_flow = self._login_flow_for_case(case)
        if login_flow is None:
            return
        logtags.trace(
            f"{case.test_id}: checking login after restart",
            logtags.VERIFY,
        )
        ensure_without_relaunch = getattr(
            login_flow, "ensure_ready_without_relaunch", None
        )
        login_ready = (
            ensure_without_relaunch()
            if callable(ensure_without_relaunch)
            else login_flow.ensure_ready()
        )
        if not login_ready:
            reason = getattr(login_flow, "last_failure_reason", None)
            detail = f": {reason}" if reason else ""
            raise AndroidOperationalError(
                f"login preparation failed after restart{detail}"
            )

    def _prepare_supported_link_if_needed(self, deep_link: str) -> None:
        if (
            self._needs_supported_link_replay(deep_link)
            and not self._supported_links_prepared
        ):
            self._executor.enable_supported_links(SUPPORTED_LINK_DOMAINS)
            self._supported_links_prepared = True

    def _can_restart_and_replay(self, case: DeeplinkTestCase) -> bool:
        return case.installed or self._needs_supported_link_replay(case.deep_link)

    @staticmethod
    def _is_incomplete_destination(observation: UIObservation) -> bool:
        """Recognize an app shell that has not produced an interactive composer."""
        if any(element.is_input for element in observation.elements):
            return False
        destination_labels = {"new chat", "chat", "cowork", "researcher"}
        has_destination = False
        has_incomplete_signal = False
        has_blocking_control = False
        for element in observation.elements:
            selector_text = element.selector_text
            parts = (
                selector_text.split("|")
                if selector_text == element.label
                else (selector_text,)
            )
            labels = {
                " ".join(part.casefold().split())
                for part in parts
                if part.strip()
            }
            if (
                element.enabled
                and element.clickable
                and any(label.startswith("message copilot") for label in labels)
            ):
                return False
            loading_labels = {
                label for label in labels if label.startswith("loading")
            }
            has_destination |= bool(labels & destination_labels)
            has_incomplete_signal |= "new chat" in labels or bool(loading_labels)
            shell_labels = destination_labels | loading_labels
            has_blocking_control |= (
                element.enabled
                and element.clickable
                and bool(labels)
                and not labels <= shell_labels
            )
        return (
            has_destination
            and has_incomplete_signal
            and not has_blocking_control
        )

    @staticmethod
    def _find_researcher_add_control(
        observation: UIObservation,
    ) -> UIElement | None:
        if any(element.is_input for element in observation.elements) or not any(
            "researcher" in element.label.casefold()
            for element in observation.elements
        ):
            return None
        for element in observation.elements:
            label = " ".join(element.selector_text.casefold().split())
            words = set(label.split())
            if element.clickable and element.enabled and (
                label in _RESEARCHER_ADD_VERBS
                or (
                    words & _RESEARCHER_ADD_VERBS
                    and words & {"agent", "researcher"}
                )
            ):
                return element
        return None

    def _run_attempts(
        self,
        case: DeeplinkTestCase,
        label: str,
        prepare: Callable[[int], None],
        on_start: "Callable[[], None] | None" = None,
        on_replay_recovery_failure: "Callable[[], None] | None" = None,
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
            except CredentialConfigurationError as exc:
                logtags.trace(
                    f"{case.test_id} credential configuration failed: {exc}",
                    label,
                )
                result.attempts.append(
                    AttemptResult(attempt=attempt, matched=False, reason=str(exc))
                )
                break
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
            except _ReplayRecoveryError as exc:
                if on_replay_recovery_failure is not None:
                    on_replay_recovery_failure()
                logtags.trace(
                    f"{case.test_id} verification recovery failed: {exc}",
                    label,
                )
                result.attempts.append(
                    AttemptResult(
                        attempt=attempt,
                        matched=False,
                        reason=f"verification recovery failed: {exc}",
                    )
                )
                continue
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

    def _installed_prepare(self, case: DeeplinkTestCase) -> Callable[[int], None]:
        def prepare(attempt: int) -> None:
            if attempt > 1:
                logtags.trace(
                    f"{case.test_id} retry: rebuilding ready app state",
                    logtags.INSTALLED,
                )
                self._restart_ready_and_replay(case)
                return
            logtags.trace(f"{case.test_id} opening deeplink", logtags.INSTALLED)
            self._executor.open_link(case.deep_link)

        return prepare

    def _uninstalled_prepare(
        self, case: DeeplinkTestCase
    ) -> tuple[Callable[[int], None], Callable[[], None]]:
        login_flow = self._login_flow_for_case(case)
        replay_supported_link = (
            self._installer is not None and self._can_restart_and_replay(case)
        )
        login_completed = False
        restart_replay_failed = False

        def invalidate_recovery() -> None:
            nonlocal login_completed, restart_replay_failed
            login_completed = False
            restart_replay_failed = True

        def prepare(attempt: int) -> None:
            nonlocal login_completed, restart_replay_failed
            if attempt > 1 and login_completed and replay_supported_link:
                logtags.trace(
                    f"{case.test_id} retry: preserving authenticated install",
                    logtags.UNINSTALLED,
                )
                try:
                    self._restart_ready_and_replay(case)
                except CredentialConfigurationError:
                    raise
                except RuntimeError:
                    invalidate_recovery()
                else:
                    logtags.trace(
                        f"{case.test_id} retry: replayed exact deeplink",
                        logtags.UNINSTALLED,
                    )
                    return
            if attempt > 1:
                retry_reason = (
                    "restart-and-replay recovery failed"
                    if restart_replay_failed
                    else "login was not completed"
                    if not login_completed
                    else "the deferred link cannot be replayed"
                )
                logtags.trace(
                    f"{case.test_id} retry: {retry_reason}; "
                    "recreating fresh-install state",
                    logtags.UNINSTALLED,
                )
            if self._installer is not None:
                logtags.trace(
                    f"{case.test_id} ensuring app is uninstalled",
                    logtags.UNINSTALLED,
                )
                self._installer.ensure_absent()
                self._supported_links_prepared = False
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
            if login_flow is not None:  # SAME shared login as the installed path
                logtags.trace(f"{case.test_id} ensuring login", logtags.UNINSTALLED)
                # On login failure, raise into the per-attempt setup-failure path
                # (failed attempt -> skip _verify() -> retry fresh / else FAIL)
                # instead of reporting ready.
                # Relaunch recovery is safe when this attempt will replay the
                # exact supported link after login. Other URLs still depend on
                # the store's deferred handoff and must not relaunch.
                if replay_supported_link:
                    login_ready = login_flow.ensure_ready()
                else:
                    ensure_without_relaunch = getattr(
                        login_flow, "ensure_ready_without_relaunch", None
                    )
                    login_ready = (
                        ensure_without_relaunch()
                        if callable(ensure_without_relaunch)
                        else login_flow.ensure_ready()
                    )
                if not login_ready:
                    failure_reason = getattr(
                        login_flow, "last_failure_reason", None
                    )
                    detail = (
                        f"login preparation failed: {failure_reason}"
                        if failure_reason
                        else "login preparation failed"
                    )
                    logtags.trace(
                        f"{case.test_id} {detail}", logtags.UNINSTALLED
                    )
                    logtags.trace(
                        f"{case.test_id} skipping deeplink verification",
                        logtags.UNINSTALLED,
                    )
                    raise RuntimeError(detail)
                if (
                    getattr(login_flow, "restarted_last_run", False)
                    and not replay_supported_link
                ):
                    raise RuntimeError(
                        "login recovery invalidated deferred deeplink"
                    )
                logtags.trace(f"{case.test_id} login ready", logtags.UNINSTALLED)
            login_completed = True
            restart_replay_failed = False
            if replay_supported_link:
                # Android cannot associate an App Link with a package that was
                # absent when the original URL opened. After installation and
                # login, approve the declared domain and replay the exact URL so
                # the destination intent reaches the app instead of Chrome.
                logtags.trace(
                    f"{case.test_id} enabling supported-link handoff",
                    logtags.UNINSTALLED,
                )
                self._prepare_supported_link_if_needed(case.deep_link)
                logtags.trace(
                    f"{case.test_id} replaying exact deeplink after login",
                    logtags.UNINSTALLED,
                )
                self._executor.open_link(case.deep_link)
            logtags.trace(
                f"{case.test_id} handing current UI to deeplink verification",
                logtags.UNINSTALLED,
            )

        return prepare, invalidate_recovery

    @staticmethod
    def _needs_supported_link_replay(deep_link: str) -> bool:
        hostname = (urlsplit(deep_link).hostname or "").casefold()
        return any(
            hostname == domain.casefold()
            for domain in SUPPORTED_LINK_DOMAINS
        )
