"""Deeplink per-case execution and verification."""

from __future__ import annotations

import math

import time
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import urlsplit

try:
    from ...apppilot.android import (
        AndroidOperationalError,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from ...apppilot import logtags
    from ...apppilot.models import Action, ActionKind, UIElement, UIObservation
    from ...apppilot.apk_trust import ForegroundComponent
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
except ImportError:
    from apppilot.android import (
        AndroidOperationalError,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from apppilot import logtags
    from apppilot.models import Action, ActionKind, UIElement, UIObservation
    from apppilot.apk_trust import ForegroundComponent
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
from .artifacts import FailureArtifactWriter
from .results import AttemptResult, SuiteReport, TestCaseResult
from .supported_links import SUPPORTED_LINK_DOMAINS

DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_WAIT_SECONDS = 2.0
DEFAULT_SETTLE_SECONDS = 3.0
DEFAULT_VERIFY_TIMEOUT_SECONDS = 30.0
DEFAULT_VERIFY_POLL_INTERVAL_SECONDS = 2.0
_RESEARCHER_ADD_VERBS = {"add", "get", "install"}
_TRUSTED_STORE_PACKAGES = {"com.android.vending"}


class _ReplayRecoveryError(RuntimeError):
    """A restart-and-replay recovery failed before verification could resume."""


@dataclass(frozen=True)
class _DeterministicVerdict:
    allowed: bool
    reason: str
    contradiction: bool = False

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
        deterministic_probe: object | None = None,
        expected_package_id: str | None = None,
        artifact_writer: FailureArtifactWriter | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        for name, value in (
            ("retry_wait_seconds", retry_wait_seconds),
            ("settle_seconds", settle_seconds),
            ("verify_timeout_seconds", verify_timeout_seconds),
            ("verify_poll_interval_seconds", verify_poll_interval_seconds),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        self._observer = observer
        self._executor = executor
        self._judge = judge
        self._warm_up = warm_up
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._retry_wait_seconds = retry_wait_seconds
        self._settle_seconds = settle_seconds
        self._verify_timeout_seconds = verify_timeout_seconds
        self._verify_poll_interval_seconds = verify_poll_interval_seconds
        self._monotonic = monotonic
        self._login_flow = login_flow
        self._login_flow_for_license = login_flow_for_license
        self._account_session = account_session
        self._profile_for_license = profile_for_license
        self._installer = installer
        if deterministic_probe is None and isinstance(executor, MaestroExecutor):
            deterministic_probe = executor
        if expected_package_id is None and isinstance(executor, MaestroExecutor):
            expected_package_id = executor.app_id
        self._deterministic_probe = deterministic_probe
        self._expected_package_id = expected_package_id
        self._artifact_writer = artifact_writer
        self._verification_evidence: dict[tuple[str, int], dict] = {}
        self._active_evidence: dict | None = None
        self._supported_links_prepared = False
        self._supported_link_state = None

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        from .orchestrator import DeeplinkSuiteOrchestrator

        return DeeplinkSuiteOrchestrator(self).run(cases)

    def _login_flow_for_case(
        self, case: DeeplinkTestCase
    ) -> LoginCapability | None:
        if self._login_flow_for_license is not None:
            return self._login_flow_for_license(case.license)
        return self._login_flow

    def ensure_logged_in(self, case: DeeplinkTestCase | None = None) -> bool:
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
        if self._warm_up is not None:
            self._warm_up()

    def prepare_supported_links(self) -> None:
        """Prepare installed-app link routing after login."""
        logtags.trace(
            f"Enabling {len(SUPPORTED_LINK_DOMAINS)} supported-link domain(s)",
            logtags.INSTALLED_BATCH,
        )
        self._enable_supported_links()

    def _enable_supported_links(self) -> None:
        if self._supported_link_state is None:
            capture = getattr(
                self._executor, "capture_supported_link_state", None
            )
            if callable(capture):
                self._supported_link_state = capture(SUPPORTED_LINK_DOMAINS)
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
        if self._installer is not None:
            self._installer.install_fresh()

    def ensure_clean_install_state(self) -> None:
        if self._installer is None:
            return
        is_installed = getattr(self._executor, "is_installed", None)
        if (
            self._supported_link_state is None
            and callable(is_installed)
            and is_installed() is True
        ):
            capture = getattr(
                self._executor, "capture_supported_link_state", None
            )
            if callable(capture):
                self._supported_link_state = capture(
                    SUPPORTED_LINK_DOMAINS
                )
        if self._installer.ensure_absent():
            logtags.trace("Removed existing app install", logtags.SUITE)
        else:
            logtags.trace("No existing app install to remove", logtags.SUITE)

    def open_installed_app(self) -> None:
        if self._installer is not None:
            self._installer.open()

    def run_installed_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run an INSTALLED case with ready-state restoration on retry."""
        result: TestCaseResult | None = None
        try:
            result = self._run_attempts(
                case, logtags.INSTALLED, self._installed_prepare(case)
            )
        finally:
            self._cleanup_case(case, result, logtags.INSTALLED)
        return result

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run an UNINSTALLED first-open case with phase-aware retries."""
        prepare, invalidate_recovery = self._uninstalled_prepare(case)
        result: TestCaseResult | None = None
        try:
            result = self._run_attempts(
                case,
                logtags.UNINSTALLED,
                prepare,
                on_start=lambda: logtags.trace(
                    f"{case.test_id} first-open flow - warm-up not applicable",
                    logtags.UNINSTALLED,
                ),
                on_replay_recovery_failure=invalidate_recovery,
            )
        finally:
            self._cleanup_case(case, result, logtags.UNINSTALLED)
        return result

    def _cleanup_case(
        self,
        case: DeeplinkTestCase,
        result: TestCaseResult | None,
        label: str,
    ) -> None:
        errors = []
        logtags.trace(f"{case.test_id} stopping app (case cleanup)", label)
        try:
            self._executor.stop_app()
        except (RuntimeError, OSError) as error:
            errors.append(f"stop app failed: {error}")
        clear_clipboard = getattr(self._executor, "clear_clipboard", None)
        if callable(clear_clipboard):
            try:
                if clear_clipboard() is False:
                    errors.append("clipboard cleanup failed")
            except (RuntimeError, OSError) as error:
                errors.append(f"clipboard cleanup failed: {error}")
        for error in errors:
            logtags.trace(f"{case.test_id} cleanup failed: {error}", label)
            if result is not None:
                result.record_cleanup_failure(error)

    def finalize_suite(self, report: SuiteReport) -> None:
        """Best-effort target-app restoration that never hides test failures."""
        errors = []
        try:
            self._executor.stop_app()
        except (RuntimeError, OSError) as error:
            errors.append(f"suite stop app failed: {error}")
        clear_clipboard = getattr(self._executor, "clear_clipboard", None)
        if callable(clear_clipboard):
            try:
                if clear_clipboard() is False:
                    errors.append("suite clipboard cleanup failed")
            except (RuntimeError, OSError) as error:
                errors.append(f"suite clipboard cleanup failed: {error}")
        if self._supported_link_state is not None:
            restore = getattr(
                self._executor, "restore_supported_link_state", None
            )
            if callable(restore):
                try:
                    restore(self._supported_link_state)
                except (RuntimeError, OSError) as error:
                    errors.append(
                        f"suite supported-link restoration failed: {error}"
                    )
        for error in errors:
            logtags.trace(error, logtags.SUITE)
            report.cleanup_errors.append(error)
            for result in report.results:
                result.record_cleanup_failure(error)
        self._write_failure_artifacts(report)

    def _verify(
        self,
        case: DeeplinkTestCase,
        attempt: int,
    ):
        """Poll one attempt until the expected result matches or times out."""
        evidence = {
            "deeplink": case.deep_link,
            "expected_destination": case.expected_result,
        }
        self._verification_evidence[
            (case.test_id.casefold(), attempt)
        ] = evidence
        self._active_evidence = evidence
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
                evidence["ui_observation"] = observation.describe(limit=50)
                if not researcher_add_attempted and (
                    add_researcher := self._find_researcher_add_control(observation)
                ) is not None:
                    logtags.trace(
                        f"{case.test_id}: adding Researcher agent",
                        logtags.VERIFY,
                    )
                    self._executor.execute(
                        Action(
                            ActionKind.TAP,
                            target_id=add_researcher.element_id,
                        ),
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
            deterministic = self._deterministic_verdict(
                case,
                observation,
            )
            evidence["deterministic_result"] = {
                "allowed": deterministic.allowed,
                "contradiction": deterministic.contradiction,
                "reason": deterministic.reason,
            }
            if not deterministic.allowed:
                verdict = self._mismatch(deterministic.reason)
                if deterministic.contradiction:
                    return verdict
            else:
                model_started = self._monotonic()
                verdict = self._judge.evaluate(
                    case.expected_result,
                    observation,
                )
                evidence["model_latency_seconds"] = (
                    self._monotonic() - model_started
                )
                evidence["model_decision"] = {
                    "matched": verdict.matched,
                    "reason": verdict.reason,
                }
            if verdict.matched:
                logtags.trace(
                    f"{case.test_id}: expected result matched", logtags.VERIFY
                )
                return verdict
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

    @staticmethod
    def _mismatch(reason: str):
        from .verification import ExpectationVerdict

        return ExpectationVerdict(matched=False, reason=reason)

    def _deterministic_verdict(
        self,
        case: DeeplinkTestCase,
        observation: UIObservation,
    ) -> _DeterministicVerdict:
        if self._deterministic_probe is None:
            return _DeterministicVerdict(True, "deterministic probe not configured")
        if not self._expected_package_id:
            raise AndroidOperationalError(
                "deterministic verification requires an expected package ID"
            )

        launch_component = getattr(
            self._deterministic_probe, "last_deeplink_component", None
        )
        if self._active_evidence is not None and isinstance(
            launch_component, ForegroundComponent
        ):
            self._active_evidence["delivery_component"] = {
                "package": launch_component.package_id,
                "activity": launch_component.activity,
            }
        if (
            isinstance(launch_component, ForegroundComponent)
            and launch_component.package_id != self._expected_package_id
            and (
                case.installed
                or self._can_restart_and_replay(case)
                or launch_component.package_id not in _TRUSTED_STORE_PACKAGES
            )
        ):
            return _DeterministicVerdict(
                False,
                "deeplink was delivered to an unexpected package",
                contradiction=True,
            )

        component_getter = getattr(
            self._deterministic_probe, "foreground_component", None
        )
        if not callable(component_getter):
            raise AndroidOperationalError(
                "deterministic foreground verification is unavailable"
            )
        component = component_getter()
        if not isinstance(component, ForegroundComponent):
            raise AndroidOperationalError(
                "foreground package/activity could not be determined"
            )
        if self._active_evidence is not None:
            self._active_evidence["foreground_component"] = {
                "package": component.package_id,
                "activity": component.activity,
            }
        if component.package_id != self._expected_package_id:
            return _DeterministicVerdict(
                False,
                "expected package is not foreground",
                contradiction=True,
            )
        activity_package = component.activity.split("/", 1)[0]
        if activity_package != self._expected_package_id:
            return _DeterministicVerdict(
                False,
                "foreground activity belongs to an unexpected package",
                contradiction=True,
            )
        if not observation.elements:
            return _DeterministicVerdict(False, "observed UI is empty")

        return _DeterministicVerdict(True, "deterministic checks passed")

    def _write_failure_artifacts(self, report: SuiteReport) -> None:
        if self._artifact_writer is None:
            return
        for result in report.results:
            if result.passed or result.artifact_paths:
                continue
            attempts = [
                attempt
                for attempt in result.attempts
                if not attempt.matched
            ]
            if not attempts and result.attempts:
                attempts = [result.attempts[-1]]
            for attempt in attempts:
                evidence = self._verification_evidence.get(
                    (result.case.test_id.casefold(), attempt.attempt),
                    {},
                )
                try:
                    path = self._artifact_writer.write(
                        case_id=result.case.test_id,
                        attempt=attempt.attempt,
                        expected_destination=result.case.expected_result,
                        observed_destination=attempt.reason,
                        matched=attempt.matched,
                        cleanup_status=result.cleanup_status,
                        cleanup_errors=result.cleanup_errors,
                        evidence=evidence,
                    )
                except OSError as error:
                    logtags.trace(
                        f"{result.case.test_id} failure artifact could not be "
                        f"written: {error}",
                        logtags.REPORT,
                    )
                    continue
                result.artifact_paths.append(str(path))

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

    def _restart_ready_and_replay(
        self,
        case: DeeplinkTestCase,
    ) -> None:
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

    def _ensure_login_after_restart(
        self,
        case: DeeplinkTestCase,
    ) -> None:
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

    def _prepare_supported_link_if_needed(
        self,
        deep_link: str,
    ) -> None:
        if (
            self._needs_supported_link_replay(deep_link)
            and not self._supported_links_prepared
        ):
            self._enable_supported_links()

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
        """Run case attempts with scenario-specific preparation."""
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

    def _installed_prepare(
        self, case: DeeplinkTestCase
    ) -> Callable[[int], None]:
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
            logtags.trace(f"{case.test_id} opening deeplink", logtags.UNINSTALLED)
            self._executor.open_link(case.deep_link)
            logtags.trace(
                f"{case.test_id} deeplink dispatched while app "
                "absent; deferred handoff pending",
                logtags.UNINSTALLED,
            )
            if self._installer is not None:
                logtags.trace(
                    f"{case.test_id} installing local build and opening via store button",
                    logtags.INSTALL,
                )
                self._installer.install_and_open(via_store_button=True)
                logtags.trace(f"{case.test_id} app opened", logtags.INSTALL)
            if login_flow is not None:
                logtags.trace(f"{case.test_id} ensuring login", logtags.UNINSTALLED)
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
