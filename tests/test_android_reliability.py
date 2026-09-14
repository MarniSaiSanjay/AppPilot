from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apppilot.android import (
    AndroidOperationalError,
    MaestroExecutor,
    MaestroHierarchyObserver,
)
from apppilot.agent import AppPilotAgent
from apppilot.brain import DecisionRequest, ModelDecision
from apppilot.models import (
    Action,
    ActionKind,
    CredentialInputMethod,
    CredentialKind,
    CredentialVerification,
    CredentialVerificationStatus,
    ExecutionContext,
    RuntimeContext,
    UIElement,
    UIObservation,
)
from apppilot.safety import SafetyValidator
from shared.login.flow import SharedLoginFlow
from shared.account.session import AndroidAccountSession
from shared.account.safety import AccountActionPurpose
from shared.login.goal import (
    AuthoritativeLoginGoalEvaluator,
    LLMLoginGoalEvaluator,
    SignedInCopilotGoalEvaluator,
)
from shared.login.login_decision_cache import LoginDecisionCache
from shared.model_client import ChatModelClient, ModelTransportError
from usecases.deeplink.deeplink_testcase_loader import DeeplinkTestCase
from usecases.deeplink.grouping import LicenseCaseGroup
from usecases.deeplink.orchestrator import DeeplinkSuiteOrchestrator
from usecases.deeplink.results import SuiteReport
from usecases.deeplink.runner import DeeplinkTestRunner
from usecases.deeplink.supported_links import SUPPORTED_LINK_DOMAINS
from usecases.deeplink.verification import LLMExpectationJudge


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

    @staticmethod
    def _credential_observation() -> tuple[UIElement, UIObservation]:
        target = UIElement(
            element_id="credential",
            parent_id=None,
            text="",
            accessibility_text="password",
            hint_text="Enter password",
            resource_id="i0118",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="password | Enter password",
            bounds=(100, 200, 900, 300),
        )
        return target, UIObservation((target,))

    @staticmethod
    def _username_observation() -> tuple[UIElement, UIObservation]:
        target = UIElement(
            element_id="credential",
            parent_id=None,
            text="",
            accessibility_text="username",
            hint_text="Email",
            resource_id="emailTextInput",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="username | Email",
            bounds=(100, 200, 900, 300),
        )
        return target, UIObservation((target,))

    @patch.object(MaestroExecutor, "_run_flow")
    @patch("apppilot.android.subprocess.run")
    def test_printable_username_uses_single_adb_shell_without_maestro(
        self,
        run,
        run_flow,
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        target, observation = self._username_observation()
        secret = "person@example.com"

        result = self.executor.execute_fast(
            Action(
                ActionKind.INPUT_TEXT,
                target_id=target.element_id,
                credential_kind=CredentialKind.USERNAME,
            ),
            observation,
            secret=secret,
        )

        self.assertEqual(result, CredentialInputMethod.ADB)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["adb", "-s", "emulator-5554", "shell", "sh"],
        )
        self.assertIn("input keycombination 113 29", run.call_args.kwargs["input"])
        self.assertIn("input text p", run.call_args.kwargs["input"])
        run_flow.assert_not_called()

    @patch.object(MaestroExecutor, "_run_flow")
    @patch.object(MaestroExecutor, "_clear_credential_field")
    def test_password_uses_single_exact_clipboard_flow(
        self,
        clear_field,
        run_flow,
    ) -> None:
        target, observation = self._credential_observation()
        secret = "P@ssw0rd!"

        result = self.executor.execute_fast(
            Action(
                ActionKind.INPUT_TEXT,
                target_id=target.element_id,
                credential_kind=CredentialKind.PASSWORD,
            ),
            observation,
            secret=secret,
        )

        self.assertEqual(result, CredentialInputMethod.CLIPBOARD)
        clear_field.assert_called_once_with(500, 250)
        run_flow.assert_called_once()
        flow = run_flow.call_args.args[0]
        self.assertTrue(flow.startswith("- tapOn:\n"))
        self.assertIn("${MAESTRO_APPPILOT_INPUT_SECRET}", flow)
        self.assertNotIn(secret, flow)
        self.assertEqual(run_flow.call_args.kwargs["secret"], secret)

    @patch.object(MaestroExecutor, "_run_flow")
    @patch.object(MaestroExecutor, "_clear_credential_field")
    def test_non_printable_username_falls_back_to_one_maestro_flow(
        self,
        clear_field,
        run_flow,
    ) -> None:
        target, observation = self._username_observation()

        result = self.executor.execute_fast(
            Action(
                ActionKind.INPUT_TEXT,
                target_id=target.element_id,
                credential_kind=CredentialKind.USERNAME,
            ),
            observation,
            secret="line one\nline two",
        )

        self.assertEqual(result, CredentialInputMethod.CLIPBOARD)
        clear_field.assert_called_once_with(500, 250)
        run_flow.assert_called_once()

    @patch.object(MaestroExecutor, "_clear_focused_credential_field")
    @patch.object(MaestroExecutor, "_run_flow")
    def test_selector_only_password_uses_bounded_two_flow_fallback(
        self,
        run_flow,
        clear_field,
    ) -> None:
        target = UIElement(
            element_id="credential",
            parent_id=None,
            text="",
            accessibility_text="password",
            hint_text="Enter password",
            resource_id="i0118",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="password | Enter password",
            bounds=None,
        )
        secret = "P@ssw0rd!"

        result = self.executor.execute_fast(
            Action(
                ActionKind.INPUT_TEXT,
                target_id=target.element_id,
                credential_kind=CredentialKind.PASSWORD,
            ),
            UIObservation((target,)),
            secret=secret,
        )

        self.assertEqual(result, CredentialInputMethod.CLIPBOARD)
        self.assertEqual(run_flow.call_count, 2)
        clear_field.assert_called_once_with()
        paste_flow = run_flow.call_args_list[1].args[0]
        self.assertIn("${MAESTRO_APPPILOT_INPUT_SECRET}", paste_flow)
        self.assertNotIn(secret, paste_flow)

    @staticmethod
    def _hierarchy(value: str) -> str:
        return json.dumps(
            {
                "attributes": {
                    "resource-id": "emailTextInput",
                    "text": value,
                    "accessibilityText": "Email",
                    "hintText": "Email",
                    "class": "android.widget.EditText",
                    "clickable": "true",
                    "enabled": "true",
                },
                "children": [],
            }
        )

    @staticmethod
    def _password_hierarchy(value: str) -> str:
        return json.dumps(
            {
                "attributes": {
                    "resource-id": "i0118",
                    "text": value,
                    "accessibilityText": "Password",
                    "hintText": "Password",
                    "class": "android.widget.EditText",
                    "clickable": "true",
                    "enabled": "true",
                    "password": "true",
                },
                "children": [],
            }
        )

    @patch("apppilot.android.subprocess.run")
    def test_next_observation_verifies_username_without_exposing_it(
        self,
        run,
    ) -> None:
        secret = "person@example.com"
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            self._hierarchy(secret),
            "",
        )
        observer = MaestroHierarchyObserver("emulator-5554")
        observer.expect_credential(
            CredentialKind.USERNAME,
            "emailTextInput",
            secret,
        )

        observation = observer._capture_maestro()

        self.assertEqual(
            observation.credential_verification.status,
            CredentialVerificationStatus.MATCHED,
        )
        self.assertNotIn(secret, observation.describe())
        self.assertEqual(observation.elements[0].text, "")

    @patch("apppilot.android.subprocess.run")
    def test_next_observation_reports_username_mismatch(
        self,
        run,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            self._hierarchy("mistyped@example.com"),
            "",
        )
        observer = MaestroHierarchyObserver("emulator-5554")
        observer.expect_credential(
            CredentialKind.USERNAME,
            "emailTextInput",
            "person@example.com",
        )

        observation = observer._capture_maestro()

        self.assertEqual(
            observation.credential_verification.status,
            CredentialVerificationStatus.MISMATCHED,
        )

    @patch("apppilot.android.subprocess.run")
    def test_next_observation_accepts_populated_masked_password(
        self,
        run,
    ) -> None:
        secret = "P@ssw0rd!"
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            self._password_hierarchy("•••••••••"),
            "",
        )
        observer = MaestroHierarchyObserver("emulator-5554")
        observer.expect_credential(
            CredentialKind.PASSWORD,
            "i0118",
            secret,
        )

        observation = observer._capture_maestro()

        self.assertEqual(
            observation.credential_verification.status,
            CredentialVerificationStatus.MATCHED,
        )
        self.assertNotIn(secret, observation.describe())

    @patch("apppilot.android.subprocess.run")
    def test_next_observation_rejects_wrong_masked_password_length(
        self,
        run,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            self._password_hierarchy("••••"),
            "",
        )
        observer = MaestroHierarchyObserver("emulator-5554")
        observer.expect_credential(
            CredentialKind.PASSWORD,
            "i0118",
            "P@ssw0rd!",
        )

        observation = observer._capture_maestro()

        self.assertEqual(
            observation.credential_verification.status,
            CredentialVerificationStatus.MISMATCHED,
        )

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


class AccountSessionNavigationTests(unittest.TestCase):
    @staticmethod
    def _element(
        element_id: str,
        label: str,
        *,
        parent_id: str | None = None,
        clickable: bool = True,
        bounds: tuple[int, int, int, int] | None = None,
    ) -> UIElement:
        return UIElement(
            element_id=element_id,
            parent_id=parent_id,
            text=label,
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.view.View",
            clickable=clickable,
            enabled=True,
            is_input=False,
            label=label,
            bounds=bounds,
        )

    def test_new_home_more_navigation_opens_explicit_profile_entry(self) -> None:
        more = self._element("more", "More", bounds=(0, 900, 200, 1000))
        new_chat = self._element(
            "new-chat",
            "New chat",
            bounds=(800, 900, 1000, 1000),
        )
        profile = self._element(
            "profile",
            "Profile",
            bounds=(0, 100, 1000, 200),
        )
        other = self._element(
            "other",
            "Help",
            bounds=(0, 800, 1000, 900),
        )
        settings = UIObservation(
            (self._element("account", "person@example.com"),)
        )
        observer = Mock()
        observer.observe.side_effect = [
            UIObservation((more, new_chat)),
            UIObservation((profile, other)),
            settings,
        ]
        executor = Mock()
        sleep = Mock()
        session = AndroidAccountSession(
            observer,
            executor,
            Mock(),
            sleep=sleep,
        )

        self.assertIs(session._open_settings(), settings)

        self.assertEqual(executor.execute_fast.call_count, 2)
        self.assertEqual(
            executor.execute_fast.call_args_list[0].args[0].target_id,
            more.element_id,
        )
        self.assertEqual(
            executor.execute_fast.call_args_list[1].args[0].target_id,
            profile.element_id,
        )
        self.assertEqual(sleep.call_count, 2)

    def test_account_menu_matching_does_not_confuse_add_menu(self) -> None:
        add_menu = self._element("add-menu", "Add menu")
        observation = UIObservation((add_menu,))
        session = AndroidAccountSession(Mock(), Mock(), Mock())

        self.assertIsNone(
            session._find_exact_text_control(
                observation,
                ("menu", "navigation menu", "open navigation"),
            )
        )

    def test_more_drawer_ignores_background_profiles_and_selects_settings(
        self,
    ) -> None:
        background_profiles = self._element(
            "background-profiles",
            "Work and Web profiles",
            bounds=(0, 100, 1000, 200),
        )
        settings = self._element(
            "settings",
            "Settings",
            bounds=(0, 700, 1000, 800),
        )
        observation = UIObservation((background_profiles, settings))
        session = AndroidAccountSession(Mock(), Mock(), Mock())

        self.assertIs(
            session._find_drawer_account_row(observation),
            settings,
        )

    def test_account_sheet_accepts_sign_in_with_another_account(self) -> None:
        control = self._element(
            "another-account",
            "Sign in with another account",
        )
        observation = UIObservation((control,))
        executor = Mock()
        session = AndroidAccountSession(Mock(), executor, Mock())

        session._tap(
            observation,
            control,
            AccountActionPurpose.ADD_ACCOUNT,
        )

        executor.execute_fast.assert_called_once()


class LoginCompletionTests(unittest.TestCase):
    @staticmethod
    def _goal_observation() -> UIObservation:
        return UIObservation(
            (
                UIElement(
                    element_id="continue",
                    parent_id=None,
                    text="Continue",
                    accessibility_text="",
                    hint_text="",
                    resource_id="continue_button",
                    class_name="android.widget.Button",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label="Continue",
                ),
            )
        )

    def test_successful_login_persists_matching_goal_verdict(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"reached": false, "actionable_step": true}'
                        )
                    }
                }
            ]
        }
        first_transport = Mock(return_value=response)
        second_transport = Mock(return_value=response)

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "goal-verdicts.json"
            first = LLMLoginGoalEvaluator(
                "model",
                "secret",
                transport=first_transport,
                cache_path=cache_path,
            )
            observation = self._goal_observation()
            self.assertFalse(first.is_reached("login", observation))
            first.finish_run(True)
            persisted = cache_path.read_text(encoding="utf-8")
            self.assertNotIn("Continue", persisted)
            self.assertNotIn("continue_button", persisted)

            second = LLMLoginGoalEvaluator(
                "model",
                "secret",
                transport=second_transport,
                cache_path=cache_path,
            )
            self.assertFalse(second.is_reached("login", observation))
            self.assertTrue(second.has_actionable_step(observation))

        first_transport.assert_called_once()
        second_transport.assert_called_once()

    def test_persistent_goal_cache_distinguishes_enabled_state(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"reached": false, "actionable_step": true}'
                        )
                    }
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "goal-verdicts.json"
            first = LLMLoginGoalEvaluator(
                "model",
                "secret",
                transport=Mock(return_value=response),
                cache_path=cache_path,
            )
            enabled = self._goal_observation()
            first.is_reached("login", enabled)
            first.finish_run(True)

            button = enabled.elements[0]
            disabled = UIObservation((replace(button, enabled=False),))
            second_transport = Mock(return_value=response)
            second = LLMLoginGoalEvaluator(
                "model",
                "secret",
                transport=second_transport,
                cache_path=cache_path,
            )
            second.is_reached("login", disabled)

        second_transport.assert_called_once()

    def test_in_memory_goal_cache_distinguishes_goal_text(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"reached": false, "actionable_step": true}'
                        )
                    }
                }
            ]
        }
        transport = Mock(return_value=response)
        with tempfile.TemporaryDirectory() as directory:
            evaluator = LLMLoginGoalEvaluator(
                "model",
                "secret",
                transport=transport,
                cache_path=Path(directory) / "goal-verdicts.json",
            )
            observation = self._goal_observation()

            evaluator.is_reached("first goal", observation)
            evaluator.is_reached("second goal", observation)

        self.assertEqual(transport.call_count, 2)

    def test_failed_login_does_not_persist_goal_verdict(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"reached": false, "actionable_step": true}'
                        )
                    }
                }
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "goal-verdicts.json"
            first = LLMLoginGoalEvaluator(
                "model",
                "secret",
                transport=Mock(return_value=response),
                cache_path=cache_path,
            )
            first.is_reached("login", self._goal_observation())
            first.finish_run(False)

            second_transport = Mock(return_value=response)
            second = LLMLoginGoalEvaluator(
                "model",
                "secret",
                transport=second_transport,
                cache_path=cache_path,
            )
            second.is_reached("login", self._goal_observation())

        second_transport.assert_called_once()

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

    def test_mismatched_fast_username_is_replaced_once_without_logging_value(
        self,
    ) -> None:
        username = UIElement(
            element_id="username",
            parent_id=None,
            text="",
            accessibility_text="username",
            hint_text="Email",
            resource_id="emailTextInput",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="username",
            bounds=(100, 100, 900, 200),
        )
        next_button = UIElement(
            element_id="next",
            parent_id=None,
            text="Next",
            accessibility_text="",
            hint_text="",
            resource_id="nextButton",
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Next",
            bounds=(100, 300, 900, 400),
        )
        initial = UIObservation((username, next_button))
        mismatch = UIObservation(
            (username, next_button),
            credential_verification=CredentialVerification(
                kind=CredentialKind.USERNAME,
                field_id="emailTextInput",
                element_id="username",
                status=CredentialVerificationStatus.MISMATCHED,
            ),
        )
        matched = UIObservation(
            (username, next_button),
            credential_verification=CredentialVerification(
                kind=CredentialKind.USERNAME,
                field_id="emailTextInput",
                element_id="username",
                status=CredentialVerificationStatus.MATCHED,
            ),
        )
        observer = Mock()
        observer.observe.side_effect = [
            initial,
            mismatch,
            matched,
            UIObservation(()),
        ]
        goal_evaluator = Mock()
        goal_evaluator.is_reached.side_effect = [False, False, True]
        goal_evaluator.failure_reason.return_value = None
        decision_provider = Mock()
        decision_provider.decide.side_effect = [
            ModelDecision(
                Action(
                    ActionKind.INPUT_TEXT,
                    target_id=username.element_id,
                    credential_kind=CredentialKind.USERNAME,
                ),
                "username",
                reobserve_required=False,
            ),
            ModelDecision(
                Action(ActionKind.TAP, target_id=next_button.element_id),
                "next",
                reobserve_required=False,
            ),
        ]
        executor = Mock()
        executor.execute_fast.side_effect = [
            CredentialInputMethod.ADB,
            None,
        ]
        secret = "person@example.com"
        agent = AppPilotAgent(
            observer=observer,
            goal_evaluator=goal_evaluator,
            decision_provider=decision_provider,
            safety_validator=SafetyValidator(),
            executor=executor,
            max_actions=5,
            runtime_context=RuntimeContext(
                {CredentialKind.USERNAME: secret}
            ),
        )

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(agent.run("login"))

        self.assertEqual(observer.expect_credential.call_count, 2)
        observer.expect_credential.assert_called_with(
            CredentialKind.USERNAME,
            "emailTextInput",
            secret,
        )
        executor.execute_credential_fallback.assert_called_once()
        self.assertEqual(
            executor.execute_credential_fallback.call_args.args[2],
            secret,
        )
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual(decision_provider.decide.call_count, 2)

    def test_unavailable_username_check_does_not_fail_on_password_screen(
        self,
    ) -> None:
        username = UIElement(
            element_id="username",
            parent_id=None,
            text="",
            accessibility_text="username",
            hint_text="Email",
            resource_id="emailTextInput",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="username",
        )
        password = UIElement(
            element_id="password",
            parent_id=None,
            text="",
            accessibility_text="password",
            hint_text="Password",
            resource_id="i0118",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="password",
        )
        observer = Mock()
        observer.observe.side_effect = [
            UIObservation((username,)),
            UIObservation(
                (password,),
                credential_verification=CredentialVerification(
                    kind=CredentialKind.USERNAME,
                    field_id="emailTextInput",
                    element_id=None,
                    status=CredentialVerificationStatus.UNAVAILABLE,
                ),
            ),
            UIObservation(
                (),
                credential_verification=CredentialVerification(
                    kind=CredentialKind.PASSWORD,
                    field_id="i0118",
                    element_id=None,
                    status=CredentialVerificationStatus.UNAVAILABLE,
                ),
            ),
        ]
        goal_evaluator = Mock()
        goal_evaluator.is_reached.side_effect = [False, False, True]
        goal_evaluator.failure_reason.return_value = None
        decision_provider = Mock()
        decision_provider.decide.side_effect = [
            ModelDecision(
                Action(
                    ActionKind.INPUT_TEXT,
                    target_id=username.element_id,
                    credential_kind=CredentialKind.USERNAME,
                ),
                "username",
                reobserve_required=False,
            ),
            ModelDecision(
                Action(
                    ActionKind.INPUT_TEXT,
                    target_id=password.element_id,
                    credential_kind=CredentialKind.PASSWORD,
                ),
                "password",
                reobserve_required=False,
            ),
        ]
        executor = Mock()
        executor.execute_fast.side_effect = [
            CredentialInputMethod.ADB,
            CredentialInputMethod.CLIPBOARD,
        ]
        agent = AppPilotAgent(
            observer=observer,
            goal_evaluator=goal_evaluator,
            decision_provider=decision_provider,
            safety_validator=SafetyValidator(),
            executor=executor,
            max_actions=4,
            runtime_context=RuntimeContext(
                {
                    CredentialKind.USERNAME: "person@example.com",
                    CredentialKind.PASSWORD: "P@ssw0rd!",
                }
            ),
        )

        with redirect_stdout(io.StringIO()):
            self.assertTrue(agent.run("login"))

        executor.execute_credential_fallback.assert_not_called()

    def test_clipboard_recovery_fails_after_one_unverified_retry(self) -> None:
        username = UIElement(
            element_id="username",
            parent_id=None,
            text="",
            accessibility_text="username",
            hint_text="Email",
            resource_id="emailTextInput",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="username",
        )
        initial = UIObservation((username,))
        mismatch = UIObservation(
            (username,),
            credential_verification=CredentialVerification(
                kind=CredentialKind.USERNAME,
                field_id="emailTextInput",
                element_id="username",
                status=CredentialVerificationStatus.MISMATCHED,
            ),
        )
        observer = Mock()
        observer.observe.side_effect = [initial, mismatch, mismatch]
        goal_evaluator = Mock()
        goal_evaluator.is_reached.return_value = False
        goal_evaluator.failure_reason.return_value = None
        decision_provider = Mock()
        decision_provider.decide.return_value = ModelDecision(
            Action(
                ActionKind.INPUT_TEXT,
                target_id=username.element_id,
                credential_kind=CredentialKind.USERNAME,
            ),
            "username",
            reobserve_required=False,
        )
        executor = Mock()
        executor.execute_fast.return_value = CredentialInputMethod.ADB
        agent = AppPilotAgent(
            observer=observer,
            goal_evaluator=goal_evaluator,
            decision_provider=decision_provider,
            safety_validator=SafetyValidator(),
            executor=executor,
            max_actions=4,
            runtime_context=RuntimeContext(
                {CredentialKind.USERNAME: "person@example.com"}
            ),
        )

        with redirect_stdout(io.StringIO()):
            self.assertFalse(agent.run("login"))

        self.assertIn(
            "verification failed after exact recovery",
            agent.last_failure_reason,
        )
        executor.execute_credential_fallback.assert_called_once()

    def test_unavailable_username_check_recovers_unique_relocated_field(
        self,
    ) -> None:
        username = UIElement(
            element_id="e:shifted",
            parent_id=None,
            text="",
            accessibility_text="username",
            hint_text="Email",
            resource_id="",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="username",
        )
        observation = UIObservation(
            (username,),
            credential_verification=CredentialVerification(
                kind=CredentialKind.USERNAME,
                field_id="e:original",
                element_id=None,
                status=CredentialVerificationStatus.UNAVAILABLE,
            ),
        )

        action = AppPilotAgent._credential_recovery_action(observation)

        self.assertEqual(action.target_id, "e:shifted")
        self.assertEqual(action.credential_kind, CredentialKind.USERNAME)

    def test_credential_recovery_respects_action_limit(self) -> None:
        username = UIElement(
            element_id="username",
            parent_id=None,
            text="",
            accessibility_text="username",
            hint_text="Email",
            resource_id="emailTextInput",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="username",
        )
        initial = UIObservation((username,))
        mismatch = UIObservation(
            (username,),
            credential_verification=CredentialVerification(
                kind=CredentialKind.USERNAME,
                field_id="emailTextInput",
                element_id="username",
                status=CredentialVerificationStatus.MISMATCHED,
            ),
        )
        observer = Mock()
        observer.observe.side_effect = [initial, mismatch]
        goal_evaluator = Mock()
        goal_evaluator.is_reached.return_value = False
        goal_evaluator.failure_reason.return_value = None
        decision_provider = Mock()
        decision_provider.decide.return_value = ModelDecision(
            Action(
                ActionKind.INPUT_TEXT,
                target_id=username.element_id,
                credential_kind=CredentialKind.USERNAME,
            ),
            "username",
            reobserve_required=False,
        )
        executor = Mock()
        executor.execute_fast.return_value = CredentialInputMethod.ADB
        agent = AppPilotAgent(
            observer=observer,
            goal_evaluator=goal_evaluator,
            decision_provider=decision_provider,
            safety_validator=SafetyValidator(),
            executor=executor,
            max_actions=1,
            runtime_context=RuntimeContext(
                {CredentialKind.USERNAME: "person@example.com"}
            ),
        )

        with redirect_stdout(io.StringIO()):
            self.assertFalse(agent.run("login"))

        self.assertEqual(
            agent.last_failure_reason,
            "action/step limit reached (1)",
        )
        executor.execute_credential_fallback.assert_not_called()

    def test_fast_submit_does_not_wait_after_credential_screen_changes(
        self,
    ) -> None:
        username = UIElement(
            element_id="username",
            parent_id=None,
            text="",
            accessibility_text="username",
            hint_text="Email",
            resource_id="emailTextInput",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="username",
            bounds=(100, 100, 900, 200),
        )
        next_button = UIElement(
            element_id="next",
            parent_id=None,
            text="Next",
            accessibility_text="",
            hint_text="",
            resource_id="nextButton",
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Next",
            bounds=(100, 300, 900, 400),
        )
        password = UIElement(
            element_id="password",
            parent_id=None,
            text="",
            accessibility_text="password",
            hint_text="Enter password",
            resource_id="i0118",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="password | Enter password",
            bounds=(100, 100, 900, 200),
        )
        sign_in = UIElement(
            element_id="sign-in",
            parent_id=None,
            text="Sign in",
            accessibility_text="",
            hint_text="",
            resource_id="idSIButton9",
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Sign in",
            bounds=(100, 300, 900, 400),
        )
        observer = Mock()
        observer.observe.side_effect = [
            UIObservation((username, next_button)),
            UIObservation((password, sign_in)),
            UIObservation(()),
        ]
        goal_evaluator = Mock()
        goal_evaluator.is_reached.side_effect = [False, False, True]
        goal_evaluator.failure_reason.return_value = None
        decision_provider = Mock()
        decision_provider.decide.side_effect = [
            ModelDecision(
                Action(ActionKind.TAP, target_id=next_button.element_id),
                "next",
                reobserve_required=False,
            ),
            ModelDecision(
                Action(
                    ActionKind.INPUT_TEXT,
                    target_id=password.element_id,
                    credential_kind=CredentialKind.PASSWORD,
                ),
                "password",
                reobserve_required=False,
            ),
        ]
        executor = Mock()
        sleep = Mock()
        agent = AppPilotAgent(
            observer=observer,
            goal_evaluator=goal_evaluator,
            decision_provider=decision_provider,
            safety_validator=SafetyValidator(),
            executor=executor,
            max_actions=5,
            runtime_context=RuntimeContext(
                {CredentialKind.PASSWORD: "P@ssw0rd!"}
            ),
            sleep=sleep,
        )

        with redirect_stdout(io.StringIO()):
            self.assertTrue(agent.run("login"))

        sleep.assert_not_called()
        self.assertEqual(executor.execute_fast.call_count, 2)

    def test_fast_submit_waits_when_same_submit_control_becomes_disabled(
        self,
    ) -> None:
        password = UIElement(
            element_id="password",
            parent_id=None,
            text="",
            accessibility_text="password",
            hint_text="Enter password",
            resource_id="passwordEntry",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="password",
            bounds=(100, 100, 900, 200),
        )
        next_button = UIElement(
            element_id="next",
            parent_id=None,
            text="Next",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Next",
            bounds=(100, 300, 900, 400),
        )
        disabled_next = UIElement(
            element_id="next",
            parent_id=None,
            text="",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.widget.Button",
            clickable=True,
            enabled=False,
            is_input=False,
            label="",
            bounds=(100, 300, 900, 400),
        )
        observer = Mock()
        observer.observe.side_effect = [
            UIObservation((password, next_button)),
            UIObservation((password, next_button)),
            UIObservation((password, disabled_next)),
            UIObservation(()),
        ]
        goal_evaluator = Mock()
        goal_evaluator.is_reached.side_effect = [False, False, False, True]
        goal_evaluator.failure_reason.return_value = None
        decision_provider = Mock()
        decision_provider.decide.side_effect = [
            ModelDecision(
                Action(
                    ActionKind.INPUT_TEXT,
                    target_id=password.element_id,
                    credential_kind=CredentialKind.PASSWORD,
                ),
                "password",
                reobserve_required=False,
            ),
            ModelDecision(
                Action(ActionKind.TAP, target_id=next_button.element_id),
                "next",
                reobserve_required=False,
            ),
        ]
        executor = Mock()
        sleep = Mock()
        agent = AppPilotAgent(
            observer=observer,
            goal_evaluator=goal_evaluator,
            decision_provider=decision_provider,
            safety_validator=SafetyValidator(),
            executor=executor,
            max_actions=5,
            runtime_context=RuntimeContext(
                {CredentialKind.PASSWORD: "P@ssw0rd!"}
            ),
            sleep=sleep,
        )

        with redirect_stdout(io.StringIO()):
            self.assertTrue(agent.run("login"))

        sleep.assert_called_once_with(0.5)
        self.assertEqual(executor.execute_fast.call_count, 2)
        self.assertEqual(decision_provider.decide.call_count, 2)

    def test_post_submit_blank_screen_uses_shorter_recovery_budget(self) -> None:
        password = UIElement(
            element_id="password",
            parent_id=None,
            text="",
            accessibility_text="password",
            hint_text="Enter password",
            resource_id="passwordEntry",
            class_name="android.widget.EditText",
            clickable=True,
            enabled=True,
            is_input=True,
            label="password",
        )
        next_button = UIElement(
            element_id="next",
            parent_id=None,
            text="Next",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Next",
        )
        observer = Mock()
        observer.observe.side_effect = [
            UIObservation((password, next_button)),
            UIObservation((password, next_button)),
            UIObservation(()),
            UIObservation(()),
            UIObservation(()),
            UIObservation(()),
            UIObservation(()),
        ]
        goal_evaluator = Mock()
        goal_evaluator.is_reached.return_value = False
        goal_evaluator.failure_reason.return_value = None
        decision_provider = Mock()
        decision_provider.decide.side_effect = [
            ModelDecision(
                Action(
                    ActionKind.INPUT_TEXT,
                    target_id=password.element_id,
                    credential_kind=CredentialKind.PASSWORD,
                ),
                "password",
                reobserve_required=False,
            ),
            ModelDecision(
                Action(ActionKind.TAP, target_id=next_button.element_id),
                "next",
                reobserve_required=False,
            ),
        ]
        executor = Mock()
        sleep = Mock()
        agent = AppPilotAgent(
            observer=observer,
            goal_evaluator=goal_evaluator,
            decision_provider=decision_provider,
            safety_validator=SafetyValidator(),
            executor=executor,
            max_actions=5,
            runtime_context=RuntimeContext(
                {CredentialKind.PASSWORD: "P@ssw0rd!"}
            ),
            sleep=sleep,
        )

        with redirect_stdout(io.StringIO()):
            self.assertFalse(agent.run("login"))

        self.assertIn("after 5 wait(s)", agent.last_failure_reason)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(decision_provider.decide.call_count, 2)

    def test_notification_permission_modal_is_actionable_and_denied(
        self,
    ) -> None:
        message = UIElement(
            element_id="message",
            parent_id="dialog",
            text="Allow Copilot to send you notifications?",
            accessibility_text="",
            hint_text="",
            resource_id="com.android.permissioncontroller:id/permission_message",
            class_name="android.widget.TextView",
            clickable=False,
            enabled=True,
            is_input=False,
            label="Allow Copilot to send you notifications?",
        )
        allow = UIElement(
            element_id="allow",
            parent_id="dialog",
            text="Allow",
            accessibility_text="",
            hint_text="",
            resource_id=(
                "com.android.permissioncontroller:id/permission_allow_button"
            ),
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Allow",
        )
        deny = UIElement(
            element_id="deny",
            parent_id="dialog",
            text="Don’t allow",
            accessibility_text="",
            hint_text="",
            resource_id=(
                "com.android.permissioncontroller:id/permission_deny_button"
            ),
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Don’t allow",
        )
        dialog = UIElement(
            element_id="dialog",
            parent_id=None,
            text="",
            accessibility_text="",
            hint_text="",
            resource_id="com.android.permissioncontroller:id/grant_dialog",
            class_name="android.widget.FrameLayout",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Allow Copilot to send you notifications? | Allow | Don’t allow",
        )
        singleton = UIElement(
            element_id="singleton",
            parent_id=None,
            text="",
            accessibility_text="",
            hint_text="",
            resource_id="com.android.permissioncontroller:id/grant_singleton",
            class_name="android.widget.FrameLayout",
            clickable=True,
            enabled=True,
            is_input=False,
            label="Allow Copilot to send you notifications? | Allow | Don’t allow",
        )
        observation = UIObservation((message, allow, deny, dialog, singleton))
        evaluator = SignedInCopilotGoalEvaluator(
            foreground_check=lambda: False
        )

        self.assertFalse(evaluator.deterministic_verdict(observation))
        self.assertTrue(
            evaluator.deterministic_actionable_verdict(observation)
        )
        self.assertTrue(evaluator.has_actionable_step(observation))

        validator = SafetyValidator()
        actions = validator.available_actions(observation)
        self.assertNotIn(
            Action(ActionKind.TAP, target_id=allow.element_id),
            actions,
        )
        deny_action = Action(ActionKind.TAP, target_id=deny.element_id)
        self.assertIn(deny_action, actions)

        fallback = Mock()
        cache = LoginDecisionCache(fallback)
        decision = cache.decide(
            DecisionRequest(
                goal="login",
                guidance=None,
                observation=observation,
                available_actions=actions,
                context=ExecutionContext(step=0, max_steps=5),
            )
        )

        self.assertEqual(decision.action, deny_action)
        self.assertFalse(decision.reobserve_required)
        fallback.decide.assert_not_called()

    def test_privacy_diagnostic_sheet_is_deterministic_and_cached(
        self,
    ) -> None:
        title = UIElement(
            element_id="title",
            parent_id=None,
            text="Your privacy matters",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.widget.TextView",
            clickable=False,
            enabled=True,
            is_input=False,
            label="Your privacy matters",
        )
        detail = UIElement(
            element_id="detail",
            parent_id=None,
            text="Diagnostic data for Microsoft 365",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.widget.TextView",
            clickable=False,
            enabled=True,
            is_input=False,
            label="Diagnostic data for Microsoft 365",
        )
        ok = UIElement(
            element_id="ok",
            parent_id=None,
            text="OK",
            accessibility_text="",
            hint_text="",
            resource_id="",
            class_name="android.widget.Button",
            clickable=True,
            enabled=True,
            is_input=False,
            label="OK",
        )
        observation = UIObservation((title, detail, ok))
        evaluator = SignedInCopilotGoalEvaluator()

        self.assertFalse(evaluator.deterministic_verdict(observation))
        self.assertTrue(
            evaluator.deterministic_actionable_verdict(observation)
        )

        validator = SafetyValidator()
        actions = validator.available_actions(observation)
        fallback = Mock()
        decision = LoginDecisionCache(fallback).decide(
            DecisionRequest(
                goal="login",
                guidance=None,
                observation=observation,
                available_actions=actions,
                context=ExecutionContext(step=0, max_steps=5),
            )
        )

        self.assertEqual(
            decision.action,
            Action(ActionKind.TAP, target_id=ok.element_id),
        )
        self.assertFalse(decision.reobserve_required)
        fallback.decide.assert_not_called()

    def test_known_onboarding_dialogs_are_deterministic_and_seeded(
        self,
    ) -> None:
        for title, control in (
            ("Privacy Settings Applied", "OK"),
            ("Meet the latest Copilot", "Continue"),
        ):
            with self.subTest(title=title):
                title_element = UIElement(
                    element_id="title",
                    parent_id=None,
                    text=title,
                    accessibility_text="",
                    hint_text="",
                    resource_id="title",
                    class_name="android.widget.TextView",
                    clickable=False,
                    enabled=True,
                    is_input=False,
                    label=title,
                )
                button = UIElement(
                    element_id="button",
                    parent_id=None,
                    text=control,
                    accessibility_text="",
                    hint_text="",
                    resource_id="android:id/button1",
                    class_name="android.widget.Button",
                    clickable=True,
                    enabled=True,
                    is_input=False,
                    label=control,
                )
                observation = UIObservation((title_element, button))
                deterministic = SignedInCopilotGoalEvaluator(
                    foreground_check=lambda: True
                )
                evaluator = AuthoritativeLoginGoalEvaluator(
                    deterministic,
                    None,
                )
                fallback = Mock()
                cache = LoginDecisionCache(fallback)
                actions = SafetyValidator().available_actions(observation)

                self.assertFalse(evaluator.is_reached("login", observation))
                self.assertTrue(evaluator.has_actionable_step(observation))
                decision = cache.decide(
                    DecisionRequest(
                        goal="login",
                        guidance=None,
                        observation=observation,
                        available_actions=actions,
                        context=ExecutionContext(step=0, max_steps=5),
                    )
                )

                self.assertEqual(decision.action.target_id, "button")
                fallback.decide.assert_not_called()


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
            deep_link="https://m365.cloud.microsoft/chat/payload",
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
            SUPPORTED_LINK_DOMAINS
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
                    SUPPORTED_LINK_DOMAINS
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
            SUPPORTED_LINK_DOMAINS
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
            deep_link="https://unsupported.example.test/apps",
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
            deep_link="https://unsupported.example.test/apps",
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

    def test_expectation_judge_caches_unchanged_observation(self) -> None:
        transport = Mock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"match": False, "reason": "wrong destination"}
                            )
                        }
                    }
                ]
            }
        )
        judge = LLMExpectationJudge(
            model="test-model",
            api_key="test-key",
            transport=transport,
        )
        observation = self._input_observation()

        with redirect_stdout(io.StringIO()):
            first = judge.evaluate("Researcher with prompt", observation)
            second = judge.evaluate("Researcher with prompt", observation)
            judge.evaluate("Chat with prompt", observation)
            judge.evaluate("Chat with prompt", UIObservation(()))

        self.assertEqual(first, second)
        self.assertEqual(transport.call_count, 3)

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
            SUPPORTED_LINK_DOMAINS
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
            SUPPORTED_LINK_DOMAINS
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
