"""Work-owned AppPilot settings loaded for every local runtime."""

from __future__ import annotations

import os
from collections.abc import MutableMapping

from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


WORK_DEFAULTS = {
    "APPPILOT_MODEL": "gpt-4.1-mini",
    "APPPILOT_MODEL_BASE_URL": (
        "https://aoai-apppilot.openai.azure.com/openai/v1"
    ),
    "APPPILOT_EXPECTED_PACKAGE_ID": "com.microsoft.office.officehubrow",
    "APPPILOT_TRUSTED_CERT_SHA256": (
        "3c4049cdcbf198a6dd4c5b956963e36c48c807b3c29e2e2c61d44513f4c1e840"
    ),
    "APPPILOT_EMAIL_API_URL": (
        "https://ca-apppilot-relay.ambitioussmoke-8a5df56b."
        "southindia.azurecontainerapps.io/send-report"
    ),
    "APPPILOT_TELEMETRY_URL": (
        "https://ca-apppilot-relay.ambitioussmoke-8a5df56b."
        "southindia.azurecontainerapps.io/telemetry"
    ),
}

RELAY_KEY_VAULT_URL = "https://kvapppilotplg9b1.vault.azure.net/"

_RELAY_SECRET_NAMES = {
    "APPPILOT_EMAIL_API_KEY": "apppilot-email-relay-key",  # allow-secret
    "APPPILOT_TELEMETRY_API_KEY": "apppilot-telemetry-relay-key",  # allow-secret
}


class RelayConfigurationError(RuntimeError):
    """Raised when AppPilot relay credentials cannot be loaded securely."""


def load_work_defaults(
    env: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Apply AppPilot-owned settings without accepting local overrides."""
    target = os.environ if env is None else env
    target.update(WORK_DEFAULTS)
    return target


def load_relay_secrets(
    *,
    credential=None,
    client_factory=SecretClient,
) -> dict[str, str]:
    """Load relay credentials from the AppPilot work Key Vault."""
    try:
        client = client_factory(
            vault_url=RELAY_KEY_VAULT_URL,
            credential=credential or DefaultAzureCredential(),
        )
        values = {
            environment_name: client.get_secret(secret_name).value or ""
            for environment_name, secret_name in _RELAY_SECRET_NAMES.items()
        }
    except AzureError as error:
        raise RelayConfigurationError(
            "AppPilot relay credentials could not be loaded from Key Vault"
        ) from error
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RelayConfigurationError(
            "AppPilot Key Vault is missing relay credential(s): "
            + ", ".join(missing)
        )
    return values
