from __future__ import annotations

import unittest

from apppilot.work_defaults import (
    RELAY_KEY_VAULT_URL,
    RelayConfigurationError,
    load_relay_secrets,
)


class _Secret:
    def __init__(self, value: str) -> None:
        self.value = value


class _Client:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.names: list[str] = []

    def get_secret(self, name: str) -> _Secret:
        self.names.append(name)
        return _Secret(self.values[name])


class WorkVaultTests(unittest.TestCase):
    def test_load_relay_secrets_uses_the_apppilot_vault(self) -> None:
        client = _Client(
            {
                "apppilot-email-relay-key": "email-key",
                "apppilot-telemetry-relay-key": "telemetry-key",
            }
        )
        calls = []

        values = load_relay_secrets(
            credential=object(),
            client_factory=lambda **kwargs: calls.append(kwargs) or client,
        )

        self.assertEqual(calls[0]["vault_url"], RELAY_KEY_VAULT_URL)
        self.assertEqual(
            values,
            {
                "APPPILOT_EMAIL_API_KEY": "email-key",
                "APPPILOT_TELEMETRY_API_KEY": "telemetry-key",
            },
        )
        self.assertEqual(
            client.names,
            [
                "apppilot-email-relay-key",
                "apppilot-telemetry-relay-key",
            ],
        )

    def test_load_relay_secrets_rejects_missing_secret_values(self) -> None:
        client = _Client(
            {
                "apppilot-email-relay-key": "email-key",
                "apppilot-telemetry-relay-key": "",
            }
        )

        with self.assertRaisesRegex(RelayConfigurationError, "TELEMETRY"):
            load_relay_secrets(
                credential=object(),
                client_factory=lambda **_kwargs: client,
            )
