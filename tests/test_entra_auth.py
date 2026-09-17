from __future__ import annotations

import argparse
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import apppilot_run
from apppilot import init as apppilot_init
from shared.login import builder as login_builder
from shared.entra_auth import (
    ensure_entra_model_readiness,
    ensure_relay_environment,
    perform_interactive_azure_login,
    reset_azure_login_state,
)
from shared.model_client import ModelTransportError
from apppilot.work_defaults import RelayConfigurationError


_ENTRA_ENV = {
    "APPPILOT_MODEL": "gpt-4.1-mini",
    "APPPILOT_MODEL_BASE_URL": "https://model.openai.azure.com/openai/v1",
}


class EntraReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_azure_login_state()
        self.addCleanup(reset_azure_login_state)

    def test_ready_credential_does_not_prompt_or_run_azure_cli(self) -> None:
        prompt = Mock()
        runner = Mock()

        result = ensure_entra_model_readiness(
            env=_ENTRA_ENV,
            interactive=True,
            input_fn=prompt,
            runner=runner,
            verifier=Mock(),
        )

        self.assertTrue(result.required)
        self.assertTrue(result.ready)
        prompt.assert_not_called()
        runner.assert_not_called()

    def test_interactive_confirmation_runs_azure_login_then_retries(self) -> None:
        verifier = Mock(
            side_effect=[
                ModelTransportError("not signed in"),
                None,
            ]
        )
        runner = Mock(
            side_effect=[
                subprocess.CompletedProcess(
                    [], 0, stdout="previous-subscription\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0),
            ]
        )
        output: list[str] = []

        result = ensure_entra_model_readiness(
            env=_ENTRA_ENV,
            interactive=True,
            input_fn=lambda _prompt: "",
            output=output.append,
            which=lambda name: "/usr/local/bin/az" if name == "az" else None,
            runner=runner,
            verifier=verifier,
        )

        self.assertTrue(result.ready)
        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(
            runner.call_args_list,
            [
                call(
                    ["az", "account", "show", "--query", "id", "--output", "tsv"],
                    check=False,
                    capture_output=True,
                    text=True,
                ),
                call(["az", "login"], check=False),
                call(
                    [
                        "az",
                        "account",
                        "set",
                        "--subscription",
                        "previous-subscription",
                    ],
                    check=False,
                ),
            ],
        )
        self.assertIn(
            "Opening the Azure CLI sign-in flow in your browser...",
            output,
        )

    def test_noninteractive_failure_does_not_launch_login(self) -> None:
        runner = Mock()

        result = ensure_entra_model_readiness(
            env=_ENTRA_ENV,
            interactive=False,
            runner=runner,
            verifier=Mock(side_effect=ModelTransportError("not signed in")),
        )

        self.assertTrue(result.required)
        self.assertFalse(result.ready)
        self.assertIn("az login", result.detail)
        runner.assert_not_called()

    def test_subscription_restore_failure_is_reported(self) -> None:
        runner = Mock(
            side_effect=[
                subprocess.CompletedProcess(
                    [], 0, stdout="previous-subscription\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 1),
            ]
        )

        result = ensure_entra_model_readiness(
            env=_ENTRA_ENV,
            interactive=True,
            input_fn=lambda _prompt: "y",
            which=lambda name: "/usr/local/bin/az" if name == "az" else None,
            runner=runner,
            verifier=Mock(side_effect=ModelTransportError("not signed in")),
        )

        self.assertFalse(result.ready)
        self.assertIn("could not restore", result.detail)

    def test_declining_login_leaves_authentication_unavailable(self) -> None:
        runner = Mock()

        result = ensure_entra_model_readiness(
            env=_ENTRA_ENV,
            interactive=True,
            input_fn=lambda _prompt: "n",
            runner=runner,
            verifier=Mock(side_effect=ModelTransportError("not signed in")),
        )

        self.assertFalse(result.ready)
        runner.assert_not_called()


class SharedAzureLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_azure_login_state()
        self.addCleanup(reset_azure_login_state)

    def _successful_runner(self) -> Mock:
        return Mock(
            side_effect=[
                subprocess.CompletedProcess(
                    [], 0, stdout="previous-subscription\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0),
            ]
        )

    def test_relay_prompts_and_retries_after_shared_login(self) -> None:
        loader = Mock(
            side_effect=[
                RelayConfigurationError("Key Vault unavailable"),
                {"APPPILOT_EMAIL_API_KEY": "secret"},
            ]
        )

        env, detail = ensure_relay_environment(
            {"EXISTING": "value"},
            interactive=True,
            input_fn=lambda _prompt: "y",
            output=lambda _message: None,
            which=lambda name: "/usr/local/bin/az" if name == "az" else None,
            runner=self._successful_runner(),
            loader=loader,
        )

        self.assertIsNone(detail)
        self.assertEqual(env["APPPILOT_EMAIL_API_KEY"], "secret")
        self.assertEqual(env["EXISTING"], "value")
        self.assertEqual(loader.call_count, 2)

    def test_relay_success_never_prompts(self) -> None:
        prompt = Mock()
        runner = Mock()

        env, detail = ensure_relay_environment(
            {},
            interactive=True,
            input_fn=prompt,
            runner=runner,
            loader=lambda: {"APPPILOT_EMAIL_API_KEY": "secret"},
        )

        self.assertIsNone(detail)
        self.assertEqual(env, {"APPPILOT_EMAIL_API_KEY": "secret"})
        prompt.assert_not_called()
        runner.assert_not_called()

    def test_relay_degrades_when_login_declined(self) -> None:
        runner = Mock()

        env, detail = ensure_relay_environment(
            {"KEEP": "1"},
            interactive=True,
            input_fn=lambda _prompt: "n",
            runner=runner,
            loader=Mock(side_effect=RelayConfigurationError("Key Vault unavailable")),
        )

        self.assertEqual(env, {"KEEP": "1"})
        self.assertIn("Key Vault unavailable", detail)
        runner.assert_not_called()

    def test_relay_noninteractive_does_not_launch_login(self) -> None:
        runner = Mock()

        env, detail = ensure_relay_environment(
            {},
            interactive=False,
            runner=runner,
            loader=Mock(side_effect=RelayConfigurationError("Key Vault unavailable")),
        )

        self.assertIn("Key Vault unavailable", detail)
        runner.assert_not_called()

    def test_single_login_satisfies_model_and_relay(self) -> None:
        runner = self._successful_runner()
        loader = Mock(
            side_effect=[
                RelayConfigurationError("Key Vault unavailable"),
                {"APPPILOT_EMAIL_API_KEY": "secret"},
            ]
        )

        _, relay_detail = ensure_relay_environment(
            {},
            interactive=True,
            input_fn=lambda _prompt: "y",
            output=lambda _message: None,
            which=lambda name: "/usr/local/bin/az" if name == "az" else None,
            runner=runner,
            loader=loader,
        )
        self.assertIsNone(relay_detail)

        # A subsequent model readiness check reuses the same sign-in without
        # prompting or invoking the Azure CLI again.
        second_prompt = Mock()
        second_runner = Mock()
        readiness = ensure_entra_model_readiness(
            env=_ENTRA_ENV,
            interactive=True,
            input_fn=second_prompt,
            runner=second_runner,
            verifier=Mock(side_effect=[ModelTransportError("not signed in"), None]),
        )

        self.assertTrue(readiness.ready)
        second_prompt.assert_not_called()
        second_runner.assert_not_called()

    def test_login_outcome_reports_missing_azure_cli(self) -> None:
        outcome = perform_interactive_azure_login(
            "the model",
            interactive=True,
            input_fn=lambda _prompt: "y",
            which=lambda _name: None,
            runner=Mock(),
        )

        self.assertFalse(outcome.ok)
        self.assertIn("not installed", outcome.detail)


class EntraCommandIntegrationTests(unittest.TestCase):
    def test_init_surfaces_entra_readiness_result(self) -> None:
        with patch.object(
            apppilot_init,
            "ensure_entra_model_readiness",
            return_value=Mock(required=True, ready=True),
        ):
            result = apppilot_init.check_entra_model_authentication(
                _ENTRA_ENV,
                interactive=False,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.status, apppilot_init.Status.PASS)
        self.assertEqual(result.name, "entra-auth")

    def test_run_stops_before_usecase_selection_when_entra_is_unavailable(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(apppilot_run, "_load_dotenv"),
            patch.object(
                apppilot_run,
                "ensure_entra_model_readiness",
                return_value=Mock(
                    required=True,
                    ready=False,
                    detail="Run `az login`, then retry.",
                ),
            ),
            patch.object(apppilot_run, "_select_usecase") as select_usecase,
            patch("sys.stderr", stderr),
        ):
            result = apppilot_run.main(["--usecase", "deeplink"])

        self.assertEqual(result, 2)
        self.assertIn("Microsoft Entra model authentication unavailable", stderr.getvalue())
        select_usecase.assert_not_called()

    def test_run_help_does_not_trigger_entra_preflight(self) -> None:
        usecase_main = Mock(return_value=0)
        with (
            patch.object(apppilot_run, "_load_dotenv"),
            patch.object(apppilot_run, "ensure_entra_model_readiness") as readiness,
            patch.object(apppilot_run, "_select_usecase", return_value="deeplink"),
            patch.object(apppilot_run, "_load_usecase_main", return_value=usecase_main),
        ):
            result = apppilot_run.main(["--usecase", "deeplink", "--help"])

        self.assertEqual(result, 0)
        readiness.assert_not_called()
        usecase_main.assert_called_once_with(["--help"])

    def test_direct_deeplink_run_stops_before_workbook_loading(self) -> None:
        from usecases.deeplink import cli as deeplink_cli

        stderr = io.StringIO()
        with (
            patch.object(deeplink_cli, "_load_dotenv"),
            patch.object(
                deeplink_cli,
                "ensure_entra_model_readiness",
                return_value=Mock(
                    required=True,
                    ready=False,
                    detail="Run `az login`, then retry.",
                ),
            ),
            patch.object(deeplink_cli, "load_deeplink_cases") as load_cases,
            patch("sys.stderr", stderr),
        ):
            result = deeplink_cli.main(["--excel", "suite.xlsx"])

        self.assertEqual(result, 2)
        self.assertIn("Microsoft Entra model authentication unavailable", stderr.getvalue())
        load_cases.assert_not_called()

    def test_standalone_login_stops_before_device_resolution(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(login_builder, "_load_dotenv"),
            patch.object(
                login_builder,
                "_parse_args",
                return_value=argparse.Namespace(
                    max_actions=1,
                    max_stuck_actions=1,
                    guidance="",
                    device=None,
                ),
            ),
            patch.object(
                login_builder,
                "ensure_entra_model_readiness",
                return_value=Mock(
                    required=True,
                    ready=False,
                    detail="Run `az login`, then retry.",
                ),
            ),
            patch.object(login_builder.device_config, "resolve_device") as resolve_device,
            patch("sys.stderr", stderr),
        ):
            result = login_builder.main()

        self.assertEqual(result, 2)
        self.assertIn("Microsoft Entra model authentication unavailable", stderr.getvalue())
        resolve_device.assert_not_called()


if __name__ == "__main__":
    unittest.main()
