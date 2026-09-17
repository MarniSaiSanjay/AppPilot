from __future__ import annotations

from types import SimpleNamespace
import unittest

from usecases.deeplink.cli import _resolve_credential_profiles


class DeeplinkCredentialTests(unittest.TestCase):
    def test_run_reports_missing_credentials_and_continues(self) -> None:
        messages: list[str] = []

        profiles = _resolve_credential_profiles(
            [SimpleNamespace(license="Entra Premium")],
            env={},
            output=messages.append,
        )

        self.assertEqual(profiles, {})
        self.assertEqual(
            messages,
            [
                "Required Deeplink test credentials are missing. Make sure to "
                "add the required credentials to your local .env before "
                "running."
            ],
        )
