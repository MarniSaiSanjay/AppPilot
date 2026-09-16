from __future__ import annotations

from types import SimpleNamespace
import unittest

from apppilot.models import CredentialKind
from shared.credentials import CredentialConfigurationError
from usecases.deeplink.cli import _resolve_credential_profiles


class DeeplinkCredentialTests(unittest.TestCase):
    def test_interactive_run_prompts_for_missing_workbook_profile(self) -> None:
        messages: list[str] = []

        profiles = _resolve_credential_profiles(
            [SimpleNamespace(license="Entra Premium")],
            env={},
            interactive=True,
            input_fn=lambda _prompt: "tester@example.test",
            password_fn=lambda _prompt: "test-password",
            output=messages.append,
        )

        profile = profiles["ENTRA_PREMIUM"]
        self.assertEqual(
            profile.runtime_context.resolve(CredentialKind.USERNAME),
            "tester@example.test",
        )
        self.assertEqual(
            profile.runtime_context.resolve(CredentialKind.PASSWORD),
            "test-password",
        )
        self.assertEqual(
            messages,
            ["Credentials required for workbook profile: Entra Premium"],
        )

    def test_noninteractive_run_requires_preconfigured_workbook_profile(self) -> None:
        with self.assertRaisesRegex(
            CredentialConfigurationError, "ENTRA_PREMIUM"
        ):
            _resolve_credential_profiles(
                [SimpleNamespace(license="Entra Premium")],
                env={},
                interactive=False,
            )
