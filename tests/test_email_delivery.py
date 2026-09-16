from __future__ import annotations

from unittest.mock import Mock, patch

from apppilot import email_delivery


def test_report_uses_fixed_footer_contact() -> None:
    report = Mock()
    view = Mock()
    report.to_email_report.return_value = view
    env = {
        "APPPILOT_EMAIL_API_URL": "https://relay.example.test/send-report",
        "APPPILOT_EMAIL_API_KEY": "test-email-relay-key",
    }

    with (
        patch.object(email_delivery.email_render, "build_subject"),
        patch.object(email_delivery.email_render, "build_text_body") as text,
        patch.object(email_delivery.email_render, "build_html_body") as html,
        patch.object(email_delivery, "_post_report"),
    ):
        assert email_delivery.send_suite_report(
            report,
            env=env,
            recipient="tester@example.test",
        )

    text.assert_called_once_with(view, "marnisanjay@microsoft.com")
    html.assert_called_once_with(view, "marnisanjay@microsoft.com")
