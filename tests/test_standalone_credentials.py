from __future__ import annotations

import unittest

from apppilot.models import CredentialKind
from shared.login import builder


class StandaloneCredentialTests(unittest.TestCase):
    def test_interactive_login_prompts_for_missing_credentials(self) -> None:
        context = builder._resolve_runtime_context(
            interactive=True,
            env={},
            input_fn=lambda _prompt: "tester@example.test",
            password_fn=lambda _prompt: "test-password",
            output=lambda _message: None,
        )

        self.assertEqual(
            context.resolve(CredentialKind.USERNAME), "tester@example.test"
        )
        self.assertEqual(
            context.resolve(CredentialKind.PASSWORD), "test-password"
        )

    def test_noninteractive_login_keeps_missing_credentials_missing(self) -> None:
        context = builder._resolve_runtime_context(interactive=False, env={})

        self.assertFalse(context.has(CredentialKind.USERNAME))
        self.assertFalse(context.has(CredentialKind.PASSWORD))

    def test_interactive_login_rejects_empty_credentials(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "requires a username and password"
        ):
            builder._resolve_runtime_context(
                interactive=True,
                env={},
                input_fn=lambda _prompt: "",
                password_fn=lambda _prompt: "",
                output=lambda _message: None,
            )
