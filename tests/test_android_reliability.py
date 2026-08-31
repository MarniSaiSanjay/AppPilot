from __future__ import annotations

import shlex
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apppilot.android import AndroidOperationalError, MaestroExecutor
from apppilot.models import Action, ActionKind, UIElement, UIObservation
from shared.login.flow import SharedLoginFlow
from shared.login.goal import AuthoritativeLoginGoalEvaluator
from shared.model_client import ChatModelClient, ModelTransportError
from usecases.deeplink.deeplink_testcase_loader import DeeplinkTestCase
from usecases.deeplink.grouping import LicenseCaseGroup
from usecases.deeplink.orchestrator import DeeplinkSuiteOrchestrator
from usecases.deeplink.results import SuiteReport
from usecases.deeplink.runner import DeeplinkTestRunner


class MaestroExecutorReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = MaestroExecutor("com.example.app", "emulator-5554")

    @staticmethod
    def _obstructed_tap_observation(
        *,
        resource_id: str = "",
        text: str = "",
    ) -> tuple[UIElement, UIObservation]:
        target = UIElement(
            element_id="button",
            parent_id=None,
            text=text,
            accessibility_text="",
            hint_text="",
            resource_id=resource_id,
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Not now",
            bounds=(0, 0, 100, 100),
        )
        blocker = UIElement(
            element_id="overlay",
            parent_id=None,
            text="Overlay",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Overlay",
            bounds=(0, 0, 100, 100),
        )
        return target, UIObservation((target, blocker))

    @patch("apppilot.android.subprocess.run")
    def test_lifecycle_operations_use_adb_without_maestro(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "Status: ok\n", "")
        deep_link = "https://example.test/path?value=1&source=app-pilot"

        self.executor.open_link(deep_link)
        self.executor.launch_app()
        self.executor.stop_app()
        self.executor.execute(
            Action(ActionKind.PRESS_BACK),
            UIObservation(()),
        )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(all(command[0] == "adb" for command in commands))
        self.assertIn(
            shlex.quote(deep_link),
            commands[0],
        )

    @patch("apppilot.android.time.sleep")
    @patch.object(MaestroExecutor, "_reset_adb_connection")
    @patch("apppilot.android.subprocess.run")
    def test_device_server_death_resets_adb_and_retries(
        self,
        run,
        reset_adb,
        _sleep,
    ) -> None:
        run.side_effect = [
            subprocess.CompletedProcess(
                [],
                1,
                "Device server died during 'deviceInfo': "
                "StatusRuntimeException: UNAVAILABLE",
                "",
            ),
            subprocess.CompletedProcess([], 0, "", ""),
        ]

        self.executor._run_flow("- tapOn: Open\n")

        self.assertEqual(run.call_count, 2)
        reset_adb.assert_called_once_with()

    @patch("apppilot.android.time.sleep")
    @patch.object(MaestroExecutor, "_reset_adb_connection")
    @patch("apppilot.android.subprocess.run")
    def test_bare_unavailable_error_does_not_replay_action(
        self,
        run,
        reset_adb,
        sleep,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            1,
            "StatusRuntimeException: UNAVAILABLE",
            "",
        )

        with self.assertRaisesRegex(
            RuntimeError, "Maestro action execution failed"
        ):
            self.executor._run_flow("- tapOn: Submit\n")

        run.assert_called_once()
        reset_adb.assert_not_called()
        sleep.assert_not_called()

    @patch.object(MaestroExecutor, "_tap_point")
    def test_merged_label_tap_avoids_overlapping_control(
        self,
        tap_point,
    ) -> None:
        target = UIElement(
            element_id="button",
            parent_id=None,
            text="",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Not now",
            bounds=(72, 2669, 1272, 2824),
        )
        overlapping_nav = UIElement(
            element_id="search",
            parent_id=None,
            text="Search",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Search",
            bounds=(633, 2734, 777, 2878),
        )
        observation = UIObservation((target, overlapping_nav))
        action = Action(ActionKind.TAP, target_id=target.element_id)

        for execute in (self.executor.execute, self.executor.execute_fast):
            with self.subTest(execute=execute.__name__):
                execute(action, observation)
                tap_point.assert_called_once_with(672, 2684)
                tap_point.reset_mock()

    @patch.object(MaestroExecutor, "_tap_point")
    @patch.object(
        MaestroExecutor,
        "_run_flow",
        side_effect=RuntimeError("element not found"),
    )
    def test_selector_miss_fallback_uses_safe_tap_point(
        self,
        _run_flow,
        tap_point,
    ) -> None:
        target = UIElement(
            element_id="button",
            parent_id=None,
            text="Not now",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Not now",
            bounds=(72, 2669, 1272, 2824),
        )
        blocker = UIElement(
            element_id="search",
            parent_id=None,
            text="Search",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Search",
            bounds=(633, 2734, 777, 2878),
        )
        observation = UIObservation((target, blocker))

        self.executor.execute(
            Action(ActionKind.TAP, target_id=target.element_id),
            observation,
        )

        tap_point.assert_called_once_with(672, 2684)

    def test_related_clickable_child_does_not_block_target(self) -> None:
        target = UIElement(
            element_id="button",
            parent_id=None,
            text="",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Not now",
            bounds=(0, 0, 100, 100),
        )
        child = UIElement(
            element_id="label",
            parent_id=target.element_id,
            text="Not now",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.widget.TextView",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Not now",
            bounds=(25, 25, 75, 75),
        )

        point = self.executor._safe_tap_point(
            target,
            UIObservation((target, child)),
        )

        self.assertEqual(point, (50, 50))

    def test_fully_obstructed_target_fails_instead_of_mistapping(self) -> None:
        target = UIElement(
            element_id="button",
            parent_id=None,
            text="",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Not now",
            bounds=(0, 0, 100, 100),
        )
        blocker = UIElement(
            element_id="overlay",
            parent_id=None,
            text="Overlay",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Overlay",
            bounds=(0, 0, 100, 100),
        )

        with self.assertRaisesRegex(
            AndroidOperationalError,
            "no unobstructed tap point",
        ):
            self.executor._safe_tap_point(
                target,
                UIObservation((target, blocker)),
            )

    def test_bounds_right_and_bottom_edges_are_exclusive(self) -> None:
        bounds = (0, 0, 100, 100)

        self.assertFalse(self.executor._contains(bounds, (100, 50)))
        self.assertFalse(self.executor._contains(bounds, (50, 100)))
        self.assertTrue(self.executor._contains(bounds, (99, 99)))

    def test_degenerate_target_bounds_fail_instead_of_mistapping(self) -> None:
        target = UIElement(
            element_id="button",
            parent_id=None,
            text="",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Not now",
            bounds=(50, 50, 50, 50),
        )

        with self.assertRaisesRegex(
            AndroidOperationalError,
            "invalid tappable bounds",
        ):
            self.executor._safe_tap_point(target, UIObservation((target,)))

    @patch.object(MaestroExecutor, "_tap_point")
    @patch.object(MaestroExecutor, "_run_flow")
    def test_fast_tap_uses_selector_when_coordinates_are_obstructed(
        self,
        run_flow,
        tap_point,
    ) -> None:
        target, observation = self._obstructed_tap_observation(
            resource_id="dismiss_button"
        )

        self.executor.execute_fast(
            Action(ActionKind.TAP, target_id=target.element_id),
            observation,
        )

        run_flow.assert_called_once_with(
            '- tapOn:\n    id: "dismiss_button"\n'
        )
        tap_point.assert_not_called()

    @patch.object(MaestroExecutor, "_tap_point")
    @patch.object(MaestroExecutor, "_run_flow")
    def test_fast_obstructed_tap_without_selector_fails_safely(
        self,
        run_flow,
        tap_point,
    ) -> None:
        target, observation = self._obstructed_tap_observation()

        with self.assertRaisesRegex(
            AndroidOperationalError,
            "no unobstructed tap point",
        ):
            self.executor.execute_fast(
                Action(ActionKind.TAP, target_id=target.element_id),
                observation,
            )

        run_flow.assert_not_called()
        tap_point.assert_not_called()

    @patch.object(MaestroExecutor, "_tap_point")
    @patch.object(
        MaestroExecutor,
        "_run_flow",
        side_effect=RuntimeError("element not found"),
    )
    def test_fast_selector_miss_does_not_tap_obstruction(
        self,
        _run_flow,
        tap_point,
    ) -> None:
        target, observation = self._obstructed_tap_observation(
            resource_id="dismiss_button"
        )

        with self.assertRaisesRegex(
            AndroidOperationalError,
            "no unobstructed tap point",
        ):
            self.executor.execute_fast(
                Action(ActionKind.TAP, target_id=target.element_id),
                observation,
            )

        tap_point.assert_not_called()

    @patch.object(MaestroExecutor, "_tap_point")
    @patch.object(MaestroExecutor, "_run_flow")
    def test_fast_obstructed_tap_can_use_own_text_selector(
        self,
        run_flow,
        tap_point,
    ) -> None:
        target, observation = self._obstructed_tap_observation(text="Not now")

        self.executor.execute_fast(
            Action(ActionKind.TAP, target_id=target.element_id),
            observation,
        )

        run_flow.assert_called_once_with(
            '- tapOn:\n    text: "Not now"\n'
        )
        tap_point.assert_not_called()


class LoginRecoveryTests(unittest.TestCase):
    @staticmethod
    def _agent(*results: bool, failure_reason: str) -> Mock:
        agent = Mock()
        agent._goal_evaluator = None
        agent.run.side_effect = results
        agent.last_failure_reason = failure_reason
        return agent

    def test_loading_exhaustion_restarts_once_and_reports_restart(self) -> None:
        agent = self._agent(
            False,
            True,
            failure_reason=(
                "no actionable step appeared after 10 wait(s); app stayed in "
                "a loading/transition state with no login/onboarding action to take"
            ),
        )
        recover = Mock()
        flow = SharedLoginFlow(agent, loading_recovery=recover)

        self.assertTrue(flow.ensure_ready())
        self.assertTrue(flow.restarted_last_run)
        recover.assert_called_once_with()

    def test_non_loading_failure_does_not_restart(self) -> None:
        agent = self._agent(False, failure_reason="access denied")
        recover = Mock()
        flow = SharedLoginFlow(agent, loading_recovery=recover)

        self.assertFalse(flow.ensure_ready())
        self.assertEqual(flow.last_failure_reason, "access denied")
        self.assertFalse(flow.restarted_last_run)
        recover.assert_not_called()


class LoginCompletionTests(unittest.TestCase):
    def test_completion_requires_three_consecutive_positive_observations(
        self,
    ) -> None:
        deterministic = Mock()
        deterministic.deterministic_verdict.side_effect = [
            True,
            True,
            False,
            True,
            True,
            True,
        ]
        evaluator = AuthoritativeLoginGoalEvaluator(deterministic, None)
        observation = UIObservation(())

        self.assertFalse(evaluator.is_reached("", observation))
        self.assertFalse(evaluator.is_reached("", observation))
        self.assertFalse(evaluator.is_reached("", observation))
        self.assertFalse(evaluator.is_reached("", observation))
        self.assertFalse(evaluator.is_reached("", observation))
        self.assertTrue(evaluator.is_reached("", observation))


class InstalledGroupOrderingTests(unittest.TestCase):
    def test_login_runs_again_after_account_preparation_and_warm_up(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC001",
            deep_link="https://example.test",
            user_type="Consumer",
            expected_result="Chat",
        )
        group = LicenseCaseGroup(
            profile_key="consumer",
            license_name="Consumer",
            cases=(case,),
        )
        runner = Mock()
        runner.ensure_logged_in.side_effect = [True, True]
        runner.prepare_account.return_value = Mock(succeeded=True)
        runner.run_installed_case.return_value = Mock(case=case)
        report = SuiteReport(suite_name="Deeplink")

        DeeplinkSuiteOrchestrator(runner)._run_installed_group(
            group,
            report,
            prepare_supported_links=True,
        )

        self.assertEqual(
            [call[0] for call in runner.method_calls],
            [
                "open_installed_app",
                "ensure_logged_in",
                "prepare_supported_links",
                "prepare_account",
                "run_warm_up",
                "open_installed_app",
                "ensure_logged_in",
                "run_installed_case",
            ],
        )


class UninstalledRecoveryTests(unittest.TestCase):
    def test_login_failure_preserves_agent_reason(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC009",
            deep_link="https://example.test",
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock(
            spec=[
                "ensure_ready_without_relaunch",
                "last_failure_reason",
                "restarted_last_run",
            ]
        )
        login_flow.ensure_ready_without_relaunch.return_value = False
        login_flow.last_failure_reason = "agent appears stuck"
        login_flow.restarted_last_run = False
        judge = Mock()
        runner = DeeplinkTestRunner(
            observer=Mock(),
            executor=Mock(),
            judge=judge,
            installer=Mock(),
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertEqual(
            result.attempts[0].reason,
            "login preparation failed: agent appears stuck",
        )
        judge.evaluate.assert_not_called()

    def test_restarted_login_discards_deferred_deeplink_attempt(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC001",
            deep_link="https://example.test",
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready.return_value = True
        login_flow.restarted_last_run = True
        judge = Mock()
        runner = DeeplinkTestRunner(
            observer=Mock(),
            executor=Mock(),
            judge=judge,
            installer=Mock(),
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertFalse(result.passed)
        self.assertEqual(
            result.attempts[0].reason,
            "login recovery invalidated deferred deeplink",
        )
        judge.evaluate.assert_not_called()

    def test_supported_link_is_approved_and_replayed_after_login(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC009",
            deep_link=(
                "https://unifiedlink.svc.cloud.microsoft/"
                "app/copilot/chat/payload"
            ),
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready.return_value = True
        login_flow.restarted_last_run = True
        installer = Mock()
        executor = Mock()
        lifecycle = Mock()
        lifecycle.attach_mock(executor, "executor")
        lifecycle.attach_mock(installer, "installer")
        lifecycle.attach_mock(login_flow, "login")
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=Mock(observe=Mock(return_value=UIObservation(()))),
            executor=executor,
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )
        runner.prepare_supported_links()
        lifecycle.reset_mock()

        self.assertTrue(runner.run_uninstalled_case(case).passed)
        login_flow.ensure_ready.assert_called_once_with()
        login_flow.ensure_ready_without_relaunch.assert_not_called()
        self.assertEqual(executor.open_link.call_count, 2)
        executor.enable_supported_links.assert_called_once_with(
            ("unifiedlink.svc.cloud.microsoft",)
        )
        self.assertEqual(
            lifecycle.method_calls,
            [
                unittest.mock.call.installer.ensure_absent(),
                unittest.mock.call.executor.open_link(case.deep_link),
                unittest.mock.call.installer.install_and_open(
                    via_store_button=True
                ),
                unittest.mock.call.login.ensure_ready(),
                unittest.mock.call.executor.enable_supported_links(
                    ("unifiedlink.svc.cloud.microsoft",)
                ),
                unittest.mock.call.executor.open_link(case.deep_link),
            ],
        )

    def test_deeplink_retry_preserves_authenticated_fresh_install(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC009",
            deep_link=(
                "https://unifiedlink.svc.cloud.microsoft/"
                "app/copilot/chat/payload"
            ),
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready.return_value = True
        login_flow.ensure_ready_without_relaunch.return_value = True
        login_flow.restarted_last_run = False
        installer = Mock()
        executor = Mock()
        judge = Mock()
        judge.evaluate.side_effect = [
            Mock(matched=False, reason="deeplink destination not ready"),
            Mock(matched=True, reason="matched"),
        ]
        runner = DeeplinkTestRunner(
            observer=Mock(observe=Mock(return_value=UIObservation(()))),
            executor=executor,
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=2,
            settle_seconds=0,
            verify_timeout_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertTrue(result.passed)
        installer.ensure_absent.assert_called_once_with()
        installer.install_and_open.assert_called_once_with(via_store_button=True)
        installer.open.assert_called_once_with()
        executor.stop_app.assert_called_once_with()
        executor.launch_app.assert_not_called()
        login_flow.ensure_ready.assert_called_once_with()
        login_flow.ensure_ready_without_relaunch.assert_called_once_with()
        self.assertEqual(executor.open_link.call_count, 3)
        executor.enable_supported_links.assert_called_once_with(
            ("unifiedlink.svc.cloud.microsoft",)
        )

    def test_login_failure_recreates_fresh_install_on_retry(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC009",
            deep_link=(
                "https://unifiedlink.svc.cloud.microsoft/"
                "app/copilot/chat/payload"
            ),
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready.side_effect = [False, True]
        login_flow.last_failure_reason = "loading stalled"
        login_flow.restarted_last_run = False
        installer = Mock()
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=Mock(observe=Mock(return_value=UIObservation(()))),
            executor=Mock(),
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=2,
            settle_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertTrue(result.passed)
        self.assertEqual(installer.ensure_absent.call_count, 2)
        self.assertEqual(installer.install_and_open.call_count, 2)

    def test_failed_preserved_recovery_escalates_to_fresh_install(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC009",
            deep_link=(
                "https://unifiedlink.svc.cloud.microsoft/"
                "app/copilot/chat/payload"
            ),
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready.return_value = True
        login_flow.restarted_last_run = False
        installer = Mock()
        installer.open.side_effect = AndroidOperationalError("launch failed")
        executor = Mock()
        judge = Mock()
        judge.evaluate.side_effect = [
            Mock(matched=False, reason="not ready"),
            Mock(matched=True, reason="matched"),
        ]
        runner = DeeplinkTestRunner(
            observer=Mock(observe=Mock(return_value=UIObservation(()))),
            executor=executor,
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=2,
            settle_seconds=0,
            verify_timeout_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(installer.ensure_absent.call_count, 2)
        self.assertEqual(installer.install_and_open.call_count, 2)
        installer.open.assert_called_once_with()
        executor.stop_app.assert_called_once_with()
        self.assertEqual(login_flow.ensure_ready.call_count, 2)

    def test_failed_in_attempt_recovery_escalates_next_retry_to_fresh(
        self,
    ) -> None:
        case = DeeplinkTestCase(
            test_id="TC009",
            deep_link=(
                "https://unifiedlink.svc.cloud.microsoft/"
                "app/copilot/chat/payload"
            ),
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready.return_value = True
        login_flow.restarted_last_run = False
        installer = Mock()
        installer.open.side_effect = AndroidOperationalError("launch failed")
        observer = Mock()
        observer.observe.side_effect = [
            AndroidOperationalError("observation failed"),
            UIObservation(()),
        ]
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=Mock(),
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=2,
            settle_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertTrue(result.passed)
        self.assertEqual(installer.ensure_absent.call_count, 2)
        self.assertEqual(installer.install_and_open.call_count, 2)
        installer.open.assert_called_once_with()
        self.assertEqual(login_flow.ensure_ready.call_count, 2)

    def test_unsupported_link_retry_recreates_fresh_install(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC008",
            deep_link="https://m365.cloud.microsoft/apps",
            user_type="Consumer",
            expected_result="Store",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready_without_relaunch.return_value = True
        login_flow.restarted_last_run = False
        installer = Mock()
        executor = Mock()
        incomplete = UIObservation(
            (
                UIElement(
                    element_id="new-chat",
                    parent_id=None,
                    text="New chat",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="New chat",
                    bounds=(0, 0, 100, 100),
                ),
            )
        )
        observer = Mock()
        observer.observe.side_effect = [incomplete, UIObservation(())]
        judge = Mock()
        judge.evaluate.side_effect = [
            Mock(matched=False, reason="not ready"),
            Mock(matched=True, reason="matched"),
        ]
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=2,
            settle_seconds=0,
            verify_timeout_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertTrue(result.passed)
        self.assertEqual(installer.ensure_absent.call_count, 2)
        self.assertEqual(installer.install_and_open.call_count, 2)
        installer.open.assert_not_called()
        executor.stop_app.assert_not_called()
        self.assertEqual(executor.open_link.call_count, 2)
        executor.enable_supported_links.assert_not_called()

    def test_unsupported_link_operational_failure_retries_fresh(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC008",
            deep_link="https://m365.cloud.microsoft/apps",
            user_type="Consumer",
            expected_result="Store",
            installed=False,
        )
        login_flow = Mock()
        login_flow.ensure_ready_without_relaunch.return_value = True
        login_flow.restarted_last_run = False
        installer = Mock()
        executor = Mock()
        observer = Mock()
        observer.observe.side_effect = [
            AndroidOperationalError("observation failed"),
            UIObservation(()),
        ]
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=2,
            settle_seconds=0,
        )

        result = runner.run_uninstalled_case(case)

        self.assertTrue(result.passed)
        self.assertEqual(installer.ensure_absent.call_count, 2)
        self.assertEqual(installer.install_and_open.call_count, 2)
        installer.open.assert_not_called()
        executor.stop_app.assert_not_called()
        self.assertEqual(executor.open_link.call_count, 2)

    def test_legacy_login_capability_remains_compatible(self) -> None:
        case = DeeplinkTestCase(
            test_id="TC001",
            deep_link="https://example.test",
            user_type="Consumer",
            expected_result="Chat",
            installed=False,
        )
        login_flow = Mock(spec=["ensure_ready"])
        login_flow.ensure_ready.return_value = True
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=Mock(observe=Mock(return_value=UIObservation(()))),
            executor=Mock(),
            judge=judge,
            installer=Mock(),
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )

        self.assertTrue(runner.run_uninstalled_case(case).passed)


class DeeplinkAttemptRecoveryTests(unittest.TestCase):
    @staticmethod
    def _case(*, installed: bool = True) -> DeeplinkTestCase:
        return DeeplinkTestCase(
            test_id="TC010",
            deep_link=(
                "https://unifiedlink.svc.cloud.microsoft/"
                "app/copilot/chat/payload"
            ),
            user_type="Consumer",
            expected_result="Chat Screen with no prompt",
            installed=installed,
        )

    @staticmethod
    def _input_observation() -> UIObservation:
        return UIObservation(
            (
                UIElement(
                    element_id="composer",
                    parent_id=None,
                    text="",
                    accessibility_text="Message Copilot",
                    hint_text="",
                    resource_id="",
                    class_name="android.widget.EditText",
                    clickable=True,
                    enabled=True,
                    is_input=True,
                    label="Message Copilot",
                    bounds=(0, 0, 100, 100),
                ),
            )
        )

    @staticmethod
    def _researcher_add_observation() -> UIObservation:
        return UIObservation(
            (
                UIElement(
                    element_id="researcher-title",
                    parent_id=None,
                    text="Researcher",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="Researcher",
                    bounds=(0, 0, 100, 50),
                ),
                UIElement(
                    element_id="add-researcher",
                    parent_id=None,
                    text="Add Researcher",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.widget.Button",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="Add Researcher",
                    bounds=(0, 50, 100, 100),
                ),
            )
        )

    @staticmethod
    def _incomplete_researcher_observation() -> UIObservation:
        return UIObservation(
            (
                UIElement(
                    element_id="researcher-shell",
                    parent_id=None,
                    text="Researcher",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="Researcher",
                    bounds=(0, 0, 100, 100),
                ),
                UIElement(
                    element_id="researcher-progress",
                    parent_id=None,
                    text="Loading",
                    accessibility_text="",
                    hint_text="",
                    resource_id="researcher_progress",
                    class_name="android.widget.ProgressBar",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="Loading",
                    bounds=(0, 100, 100, 200),
                ),
            )
        )

    def test_operational_failure_restarts_and_replays_within_attempt(self) -> None:
        case = self._case()
        observer = Mock()
        observer.observe.side_effect = [
            AndroidOperationalError("Maestro action timed out"),
            self._input_observation(),
        ]
        executor = Mock()
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertTrue(result.passed)
        executor.stop_app.assert_called_once_with()
        executor.enable_supported_links.assert_called_once_with(
            ("unifiedlink.svc.cloud.microsoft",)
        )
        executor.open_link.assert_called_once_with(case.deep_link)
        self.assertEqual(observer.reset_recovery_budget.call_count, 2)

    def test_supported_fresh_install_restarts_and_replays_within_attempt(
        self,
    ) -> None:
        case = self._case(installed=False)
        observer = Mock()
        observer.observe.side_effect = [
            AndroidOperationalError("Maestro action timed out"),
            self._input_observation(),
        ]
        executor = Mock()
        installer = Mock()
        login_flow = Mock()
        login_flow.ensure_ready_without_relaunch.return_value = True
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertTrue(result.passed)
        executor.stop_app.assert_called_once_with()
        installer.open.assert_called_once_with()
        executor.open_link.assert_called_once_with(case.deep_link)

    def test_incomplete_chat_shell_restarts_and_replays_once(self) -> None:
        case = self._case()
        incomplete = UIObservation(
            (
                UIElement(
                    element_id="new-chat",
                    parent_id=None,
                    text="New chat",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="New chat",
                    bounds=(0, 0, 100, 100),
                ),
            )
        )
        observer = Mock()
        observer.observe.side_effect = [incomplete, self._input_observation()]
        executor = Mock()
        judge = Mock()
        judge.evaluate.side_effect = [
            Mock(matched=False, reason="composer missing"),
            Mock(matched=True, reason="matched"),
        ]
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
            verify_timeout_seconds=0,
        )

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertTrue(result.passed)
        executor.stop_app.assert_called_once_with()
        executor.open_link.assert_called_once_with(case.deep_link)

    def test_researcher_add_is_retried_after_recovery_replay(self) -> None:
        case = self._case()
        researcher_add = self._researcher_add_observation()
        observer = Mock()
        observer.observe.side_effect = [
            researcher_add,
            self._incomplete_researcher_observation(),
            researcher_add,
            self._input_observation(),
        ]
        executor = Mock()
        judge = Mock()
        judge.evaluate.side_effect = [
            Mock(matched=False, reason="composer missing"),
            Mock(matched=True, reason="matched"),
        ]
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
            verify_timeout_seconds=0,
        )

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertTrue(result.passed)
        self.assertEqual(executor.execute.call_count, 2)
        executor.stop_app.assert_called_once_with()

    def test_researcher_add_is_retried_after_operational_recovery(self) -> None:
        case = self._case()
        researcher_add = self._researcher_add_observation()
        observer = Mock()
        observer.observe.side_effect = [
            researcher_add,
            AndroidOperationalError("Maestro action timed out"),
            researcher_add,
            self._input_observation(),
        ]
        executor = Mock()
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertTrue(result.passed)
        self.assertEqual(executor.execute.call_count, 2)
        executor.stop_app.assert_called_once_with()

    def test_clickable_composer_placeholder_is_not_incomplete(self) -> None:
        observation = UIObservation(
            (
                UIElement(
                    element_id="composer-placeholder",
                    parent_id=None,
                    text="",
                    accessibility_text="Message Copilot",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="Message Copilot",
                    bounds=(0, 0, 100, 100),
                ),
                UIElement(
                    element_id="chat",
                    parent_id=None,
                    text="Chat",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="Chat",
                    bounds=(0, 100, 100, 200),
                ),
            )
        )

        self.assertFalse(
            DeeplinkTestRunner._is_incomplete_destination(observation)
        )

    def test_destination_label_alone_is_not_incomplete(self) -> None:
        observation = UIObservation(
            (
                UIElement(
                    element_id="chat",
                    parent_id=None,
                    text="Chat",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="Chat",
                    bounds=(0, 0, 100, 100),
                ),
            )
        )

        self.assertFalse(
            DeeplinkTestRunner._is_incomplete_destination(observation)
        )

    def test_merged_shell_labels_with_static_chrome_are_incomplete(self) -> None:
        observation = UIObservation(
            (
                UIElement(
                    element_id="shell",
                    parent_id=None,
                    text="",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="Chat | New chat",
                    bounds=(0, 0, 100, 100),
                ),
                UIElement(
                    element_id="title",
                    parent_id=None,
                    text="Microsoft 365 Copilot",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="Microsoft 365 Copilot",
                    bounds=(0, 100, 100, 200),
                ),
            )
        )

        self.assertTrue(
            DeeplinkTestRunner._is_incomplete_destination(observation)
        )

    def test_disabled_composer_placeholder_is_incomplete(self) -> None:
        observation = UIObservation(
            (
                UIElement(
                    element_id="composer-placeholder",
                    parent_id=None,
                    text="",
                    accessibility_text="Message Copilot",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=True,
                    enabled=False,
                    is_input=False,
                    label="Message Copilot",
                    bounds=(0, 0, 100, 100),
                ),
                UIElement(
                    element_id="new-chat",
                    parent_id=None,
                    text="New chat",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="New chat",
                    bounds=(0, 100, 100, 200),
                ),
            )
        )

        self.assertTrue(
            DeeplinkTestRunner._is_incomplete_destination(observation)
        )

    def test_merged_enabled_composer_is_not_incomplete(self) -> None:
        observation = UIObservation(
            (
                UIElement(
                    element_id="composer-container",
                    parent_id=None,
                    text="",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="New chat | Message Copilot",
                    bounds=(0, 0, 100, 100),
                ),
            )
        )

        self.assertFalse(
            DeeplinkTestRunner._is_incomplete_destination(observation)
        )

    def test_usable_composerless_destination_is_not_incomplete(self) -> None:
        observation = UIObservation(
            (
                UIElement(
                    element_id="researcher",
                    parent_id=None,
                    text="Researcher",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.view.View",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label="Researcher",
                    bounds=(0, 0, 100, 100),
                ),
                UIElement(
                    element_id="start-research",
                    parent_id=None,
                    text="Start research",
                    accessibility_text="",
                    hint_text="",
                    resource_id="",
                    class_name="android.widget.Button",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="Start research",
                    bounds=(0, 100, 100, 200),
                ),
            )
        )

        self.assertFalse(
            DeeplinkTestRunner._is_incomplete_destination(observation)
        )

    def test_recovery_reuses_existing_supported_link_setup(self) -> None:
        case = self._case()
        observer = Mock()
        observer.observe.side_effect = [
            AndroidOperationalError("Maestro action timed out"),
            self._input_observation(),
        ]
        executor = Mock()
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )
        runner.prepare_supported_links()
        executor.reset_mock()

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertTrue(result.passed)
        executor.enable_supported_links.assert_not_called()
        executor.open_link.assert_called_once_with(case.deep_link)

    def test_recovery_checks_login_without_another_relaunch(self) -> None:
        case = self._case()
        observer = Mock()
        observer.observe.side_effect = [
            AndroidOperationalError("Maestro action timed out"),
            self._input_observation(),
        ]
        executor = Mock()
        installer = Mock()
        login_flow = Mock()
        login_flow.ensure_ready_without_relaunch.return_value = True
        judge = Mock()
        judge.evaluate.return_value = Mock(matched=True, reason="matched")
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            installer=installer,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
        )

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertTrue(result.passed)
        installer.open.assert_called_once_with()
        executor.launch_app.assert_not_called()
        login_flow.ensure_ready_without_relaunch.assert_called_once_with()
        login_flow.ensure_ready.assert_not_called()
        executor.open_link.assert_called_once_with(case.deep_link)

    def test_installed_retry_rebuilds_only_required_ready_state(self) -> None:
        case = self._case()
        observer = Mock(observe=Mock(return_value=self._input_observation()))
        executor = Mock()
        login_flow = Mock()
        login_flow.ensure_ready_without_relaunch.return_value = True
        judge = Mock()
        judge.evaluate.side_effect = [
            Mock(matched=False, reason="not ready"),
            Mock(matched=True, reason="matched"),
        ]
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            login_flow=login_flow,
            sleep=lambda _seconds: None,
            max_attempts=2,
            settle_seconds=0,
            verify_timeout_seconds=0,
        )

        result = runner.run_installed_case(case)

        self.assertTrue(result.passed)
        executor.launch_app.assert_called_once_with()
        login_flow.ensure_ready_without_relaunch.assert_called_once_with()
        login_flow.ensure_ready.assert_not_called()
        self.assertEqual(executor.open_link.call_count, 2)
        executor.enable_supported_links.assert_called_once_with(
            ("unifiedlink.svc.cloud.microsoft",)
        )

    def test_usable_wrong_destination_is_not_restarted(self) -> None:
        case = self._case()
        observer = Mock(observe=Mock(return_value=self._input_observation()))
        executor = Mock()
        judge = Mock()
        judge.evaluate.return_value = Mock(
            matched=False,
            reason="wrong destination",
        )
        runner = DeeplinkTestRunner(
            observer=observer,
            executor=executor,
            judge=judge,
            sleep=lambda _seconds: None,
            max_attempts=1,
            settle_seconds=0,
            verify_timeout_seconds=0,
        )

        result = runner._run_attempts(case, "[TEST]", Mock())

        self.assertFalse(result.passed)
        executor.stop_app.assert_not_called()
        executor.open_link.assert_not_called()


class ModelClientReliabilityTests(unittest.TestCase):
    @staticmethod
    def _response(payload: bytes) -> Mock:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = payload
        return response

    @patch("shared.model_client.time.sleep")
    @patch("shared.model_client.urllib.request.urlopen")
    def test_throttling_honors_retry_after_then_uses_backoff(
        self, urlopen, sleep
    ) -> None:
        urlopen.side_effect = [
            urllib.error.HTTPError(
                "https://example.test",
                429,
                "Too Many Requests",
                {"Retry-After": "7"},
                None,
            ),
            urllib.error.HTTPError(
                "https://example.test",
                429,
                "Too Many Requests",
                {},
                None,
            ),
            self._response(b'{"choices": []}'),
        ]

        response = ChatModelClient("model", "secret").send({"messages": []})

        self.assertEqual(response, {"choices": []})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [7.0, 4.0])

    @patch("shared.model_client.time.sleep")
    @patch("shared.model_client.urllib.request.urlopen")
    def test_malformed_response_is_not_resent(self, urlopen, sleep) -> None:
        urlopen.return_value = self._response(b"not-json")

        with self.assertRaises(ModelTransportError):
            ChatModelClient("model", "secret").send({"messages": []})

        urlopen.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
