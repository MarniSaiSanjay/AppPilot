from __future__ import annotations

import io
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from azure.core.exceptions import AzureError
from apppilot.init import Status, check_model
from usecases.deeplink import cli as deeplink_cli
from shared.model_client import (
    AZURE_OPENAI_SCOPE,
    ChatModelClient,
    ModelConfigurationError,
    ModelTransportError,
    verify_entra_model_authentication,
)


class ModelAuthenticationTests(unittest.TestCase):
    def test_work_azure_openai_configuration_requires_model_and_endpoint(self) -> None:
        config = ChatModelClient.config_from_env(
            {
                "APPPILOT_MODEL": "deployment",
                "APPPILOT_MODEL_BASE_URL": "https://model.openai.azure.com/openai/v1",
            }
        )

        self.assertEqual(config["model"], "deployment")
        self.assertEqual(
            config["base_url"], "https://model.openai.azure.com/openai/v1"
        )
        self.assertIsNone(
            ChatModelClient.config_from_env({"APPPILOT_MODEL": "deployment"})
        )

    def test_work_configuration_rejects_non_azure_base_url(self) -> None:
        with self.assertRaisesRegex(ModelConfigurationError, "openai.azure.com"):
            ChatModelClient.config_from_env(
                {
                    "APPPILOT_MODEL": "deployment",
                    "APPPILOT_MODEL_BASE_URL": "https://model.example.test/openai/v1",
                }
            )

    def test_work_configuration_rejects_non_v1_azure_openai_path(self) -> None:
        with self.assertRaisesRegex(ModelConfigurationError, "/openai/v1"):
            ChatModelClient.config_from_env(
                {
                    "APPPILOT_MODEL": "deployment",
                    "APPPILOT_MODEL_BASE_URL": "https://model.openai.azure.com/v1",
                }
            )

    def test_direct_client_rejects_non_azure_endpoint(self) -> None:
        with self.assertRaisesRegex(ModelConfigurationError, "openai.azure.com"):
            ChatModelClient(
                "deployment",
                token_provider=Mock(return_value="entra-token"),
                base_url="https://model.example.test/openai/v1",
            )

    @patch("shared.model_client._NO_REDIRECT_OPENER.open")
    def test_work_entra_token_is_sent_as_bearer_auth(self, open_request) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices": []}'
        open_request.return_value = response
        token_provider = Mock(return_value="entra-token")
        client = ChatModelClient(
            "deployment",
            token_provider=token_provider,
            base_url="https://model.openai.azure.com/openai/v1",
        )

        self.assertEqual(client.send({"messages": []}), {"choices": []})

        token_provider.assert_called_once_with()
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer entra-token")

    @patch("shared.model_client.DefaultAzureCredential")
    def test_entra_preflight_hides_credential_chain_diagnostics(
        self, credential_factory
    ) -> None:
        credential = Mock()
        credential_factory.return_value = credential
        logger = logging.getLogger("azure.identity._credentials.chained")
        handler_stream = io.StringIO()
        handler = logging.StreamHandler(handler_stream)
        previous_disabled = logger.disabled
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        def fail(scope: str) -> None:
            self.assertEqual(scope, AZURE_OPENAI_SCOPE)
            logger.warning("verbose credential-chain diagnostics")
            raise AzureError("not signed in")

        credential.get_token.side_effect = fail
        try:
            with self.assertRaisesRegex(
                ModelTransportError,
                "Microsoft Entra model authentication failed",
            ):
                verify_entra_model_authentication()
        finally:
            logger.removeHandler(handler)
            logger.disabled = previous_disabled
            logger.setLevel(previous_level)

        self.assertEqual(handler_stream.getvalue(), "")

    def test_init_accepts_work_azure_openai_configuration(self) -> None:
        result = check_model(
            {
                "APPPILOT_MODEL": "deployment",
                "APPPILOT_MODEL_BASE_URL": "https://model.openai.azure.com/openai/v1",
            }
        )

        self.assertEqual(result.status, Status.PASS)
        self.assertIn("Work Azure OpenAI", result.detail)

    def test_deeplink_cli_reports_login_model_configuration_error(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(deeplink_cli, "_load_dotenv"),
            patch.object(
                deeplink_cli, "_select_workbook", return_value=Path("suite.xlsx")
            ),
            patch.object(deeplink_cli, "load_deeplink_cases", return_value=[]),
            patch.object(
                deeplink_cli, "_resolve_credential_profiles", return_value={}
            ),
            patch.object(deeplink_cli, "_default_max_actions", return_value=1),
            patch.object(
                deeplink_cli, "_default_max_stuck_actions", return_value=1
            ),
            patch.object(
                deeplink_cli.apk_config,
                "resolve_apk_path",
                return_value=Path("app.apk"),
            ),
            patch.object(
                deeplink_cli.LLMExpectationJudge,
                "from_env",
                return_value=Mock(),
            ),
            patch.object(
                deeplink_cli.device_config,
                "resolve_device",
                return_value="device-1",
            ),
            patch.object(deeplink_cli, "MaestroHierarchyObserver"),
            patch.object(deeplink_cli, "MaestroExecutor"),
            patch.object(
                deeplink_cli,
                "resolve_decision_provider",
                side_effect=ModelConfigurationError("invalid Azure OpenAI endpoint"),
            ),
            patch("sys.stderr", stderr),
        ):
            result = deeplink_cli.main(
                ["--excel", "suite.xlsx", "--apk", "app.apk", "--no-warm-up"]
            )

        self.assertEqual(result, 2)
        self.assertIn(
            "ERROR: invalid model configuration: invalid Azure OpenAI endpoint",
            stderr.getvalue(),
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_deeplink_cli_fails_when_report_cannot_be_saved(self) -> None:
        report = Mock(suite_name="Deeplink", total=1, failed=0)
        lock = MagicMock()
        stderr = io.StringIO()
        with (
            patch.object(deeplink_cli, "_load_dotenv"),
            patch.object(
                deeplink_cli,
                "ensure_entra_model_readiness",
                return_value=Mock(required=False, ready=True),
            ),
            patch.object(
                deeplink_cli, "_select_workbook", return_value=Path("suite.xlsx")
            ),
            patch.object(deeplink_cli, "load_deeplink_cases", return_value=[]),
            patch.object(
                deeplink_cli, "_resolve_credential_profiles", return_value={}
            ),
            patch.object(deeplink_cli, "_default_max_actions", return_value=1),
            patch.object(
                deeplink_cli, "_default_max_stuck_actions", return_value=1
            ),
            patch.object(
                deeplink_cli.apk_config,
                "resolve_apk_path",
                return_value=Path("app.apk"),
            ),
            patch.object(
                deeplink_cli.LLMExpectationJudge,
                "from_env",
                return_value=Mock(),
            ),
            patch.object(
                deeplink_cli.device_config,
                "resolve_device",
                return_value="device-1",
            ),
            patch.object(deeplink_cli, "MaestroHierarchyObserver"),
            patch.object(deeplink_cli, "MaestroExecutor"),
            patch.object(deeplink_cli, "resolve_decision_provider", return_value=Mock()),
            patch.object(deeplink_cli, "load_relay_secrets", return_value={}),
            patch.object(
                deeplink_cli.email_delivery,
                "prompt_email_recipient",
                return_value=None,
            ),
            patch.object(deeplink_cli.device_lock, "DeviceProcessLock", return_value=lock),
            patch.object(deeplink_cli, "DeeplinkSuiteOrchestrator") as orchestrator,
            patch.object(
                deeplink_cli.report_storage,
                "save_markdown_report",
                side_effect=OSError("disk full"),
            ),
            patch.object(deeplink_cli.telemetry, "record_suite_run"),
            patch("sys.stderr", stderr),
        ):
            orchestrator.return_value.run.return_value = report
            result = deeplink_cli.main(
                ["--excel", "suite.xlsx", "--apk", "app.apk", "--no-warm-up"]
            )

        self.assertEqual(result, 1)
        self.assertIn("ERROR: report could not be saved: disk full", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
