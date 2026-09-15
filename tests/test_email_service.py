from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError


class EmailServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.environment = patch.dict(
            os.environ,
            {
                "ACS_CONNECTION_STRING": (
                    "endpoint=https://example.test/;"
                    "accesskey=test-access-key"
                ),
                "ACS_SENDER_ADDRESS": "sender@example.test",
                "APPPILOT_API_KEY": "email-test-key",
                "APPPILOT_EMAIL_TO": "default@example.test",
                "APPPILOT_TELEMETRY_API_KEY": "telemetry-test-key",
                "TELEMETRY_TABLE_ENDPOINT": (
                    "https://testaccount.table.core.windows.net"
                ),
                "AZURE_CLIENT_ID": "managed-identity-client-id",
            },
            clear=False,
        )
        cls.environment.start()
        cls.email_factory = patch(
            "azure.communication.email.EmailClient.from_connection_string"
        )
        cls.email_factory_mock = cls.email_factory.start()
        cls.email_client = Mock()
        cls.email_factory_mock.return_value = cls.email_client
        cls.credential_factory = patch(
            "azure.identity.DefaultAzureCredential"
        )
        cls.credential_factory_mock = cls.credential_factory.start()
        cls.credential = Mock()
        cls.credential_factory_mock.return_value = cls.credential
        sys.modules.pop("email_service.app", None)
        cls.relay = importlib.import_module("email_service.app")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.credential_factory.stop()
        cls.email_factory.stop()
        cls.environment.stop()
        sys.modules.pop("email_service.app", None)

    def setUp(self) -> None:
        self.email_client.reset_mock()

    def test_health_check_is_credential_free(self) -> None:
        self.assertEqual(self.relay.healthz(), {"status": "ok"})

    def test_email_and_telemetry_keys_must_be_distinct(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must be different") as raised:
            self.relay._validate_api_key_separation(
                "shared-key", "shared-key"
            )

        self.assertNotIn("shared-key", str(raised.exception))

    def test_empty_telemetry_key_does_not_require_separation(self) -> None:
        self.relay._validate_api_key_separation("email-key", "")

    def test_email_rejects_wrong_scoped_key(self) -> None:
        payload = self.relay.ReportIn(subject="Report", body="body")

        with self.assertRaises(HTTPException) as raised:
            self.relay.send_report(payload, x_api_key="telemetry-test-key")

        self.assertEqual(raised.exception.status_code, 401)
        self.email_client.begin_send.assert_not_called()

    def test_email_uses_fixed_sender_and_validated_recipient(self) -> None:
        poller = Mock()
        poller.result.return_value = {"id": "message-1"}
        self.email_client.begin_send.return_value = poller
        payload = self.relay.ReportIn(
            subject="Report",
            body="plain",
            html="<strong>report</strong>",
            to="recipient@example.test",
        )

        result = self.relay.send_report(
            payload, x_api_key="email-test-key"
        )

        self.assertEqual(result, {"status": "sent", "id": "message-1"})
        message = self.email_client.begin_send.call_args.args[0]
        self.assertEqual(message["senderAddress"], "sender@example.test")
        self.assertEqual(
            message["recipients"]["to"],
            [{"address": "recipient@example.test"}],
        )

    def test_provider_failure_returns_sanitized_error(self) -> None:
        self.email_client.begin_send.side_effect = RuntimeError(
            "accesskey=provider-secret"
        )
        payload = self.relay.ReportIn(subject="Report", body="body")

        with self.assertLogs("apppilot-email-relay") as logs:
            with self.assertRaises(HTTPException) as raised:
                self.relay.send_report(payload, x_api_key="email-test-key")

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "email send failed")
        self.assertNotIn("provider-secret", raised.exception.detail)
        self.assertNotIn("provider-secret", "\n".join(logs.output))

    @patch("email_service.app.TableClient")
    def test_telemetry_uses_separate_key_and_exact_schema(
        self, table_factory
    ) -> None:
        table = Mock()
        table_factory.return_value.__enter__.return_value = table
        payload = self.relay.TelemetryIn(
            suite="Deeplink",
            test_cases=48,
            host_os="macOS",
            target_platform="Android",
        )

        with self.assertRaises(HTTPException) as raised:
            self.relay.telemetry(payload, x_api_key="email-test-key")
        self.assertEqual(raised.exception.status_code, 401)

        result = self.relay.telemetry(
            payload, x_api_key="telemetry-test-key"
        )

        self.assertEqual(result, {"status": "recorded"})
        self.credential_factory_mock.assert_called_once_with(
            managed_identity_client_id="managed-identity-client-id"
        )
        table_factory.assert_called_once_with(
            endpoint="https://testaccount.table.core.windows.net",
            table_name="SuiteRuns",
            credential=self.credential,
        )
        entity = table.create_entity.call_args.args[0]
        self.assertEqual(
            set(entity),
            {
                "PartitionKey",
                "RowKey",
                "suite",
                "test_cases",
                "host_os",
                "target_platform",
            },
        )

    def test_telemetry_rejects_extra_fields(self) -> None:
        with self.assertRaises(ValidationError):
            self.relay.TelemetryIn(
                suite="Deeplink",
                test_cases=48,
                host_os="macOS",
                target_platform="Android",
                email="person@example.test",
            )

    @patch("email_service.app.TableClient")
    def test_telemetry_provider_failure_does_not_log_credentials(
        self, table_factory
    ) -> None:
        table = Mock()
        table.create_entity.side_effect = RuntimeError(
            "AccountKey=storage-provider-secret"
        )
        table_factory.return_value.__enter__.return_value = table
        payload = self.relay.TelemetryIn(
            suite="Deeplink",
            test_cases=48,
            host_os="macOS",
            target_platform="Android",
        )

        with self.assertLogs("apppilot-email-relay") as logs:
            with self.assertRaises(HTTPException) as raised:
                self.relay.telemetry(
                    payload, x_api_key="telemetry-test-key"
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "telemetry store failed")
        self.assertNotIn(
            "storage-provider-secret",
            "\n".join(logs.output),
        )


if __name__ == "__main__":
    unittest.main()
