"""Send a test-suite report by email via the AppPilot email relay."""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Optional

from . import logtags, email_render

_REQUIRED_ENV = (
    "APPPILOT_EMAIL_API_URL",
    "APPPILOT_EMAIL_API_KEY",
)

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")

_RECIPIENT_STORE = Path(__file__).resolve().parents[2] / ".apppilot_recipient"

_MAX_BODY_CHARS = 190_000

_HTTP_TIMEOUT_SECONDS = 120

Transport = Callable[[urllib.request.Request], "tuple[int, str]"]


def _log(message: str) -> None:
    print(logtags.prefix(logtags.EMAIL, message))


def _missing_config(env: Mapping[str, str]) -> "list[str]":
    return [key for key in _REQUIRED_ENV if not env.get(key)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """HTTP redirect blocker for relay requests."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _default_transport(request: urllib.request.Request) -> "tuple[int, str]":
    with _OPENER.open(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return response.status, response.read().decode("utf-8")


def _cap_body(body: str) -> str:
    """Cap the plain-text email body."""
    if len(body) > _MAX_BODY_CHARS:
        return (
            body[:_MAX_BODY_CHARS]
            + "\n\n[... report truncated for email; see console output ...]"
        )
    return body


def _cap_html(html_body: str) -> str:
    """Return HTML when it fits within the relay limit."""
    return html_body if len(html_body) <= _MAX_BODY_CHARS else ""


def _post_report(
    env: Mapping[str, str],
    subject: str,
    body: str,
    transport: Transport,
    recipient: Optional[str] = None,
    html_body: Optional[str] = None,
) -> None:
    message = {"subject": subject, "body": body}
    if recipient:
        message["to"] = recipient
    if html_body:
        message["html"] = html_body
    url = env["APPPILOT_EMAIL_API_URL"]
    if not url.lower().startswith("https://"):
        raise ValueError("APPPILOT_EMAIL_API_URL must use https")
    payload = json.dumps(message).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": env["APPPILOT_EMAIL_API_KEY"],
        },
        method="POST",
    )
    status, _ = transport(request)
    if not 200 <= status < 300:
        raise RuntimeError(f"relay responded with status {status}")


def send_suite_report(
    report,
    *,
    env: Mapping[str, str],
    recipient: Optional[str] = None,
    transport: Transport = _default_transport,
) -> bool:
    """Email the final suite report through the relay."""
    missing = _missing_config(env)
    if missing:
        _log(f"report NOT sent - missing configuration: {', '.join(missing)}")
        return False
    destination = recipient or "the configured default recipient"
    _log(f"sending report to {destination} - this can take up to a minute...")
    contact = (env.get("APPPILOT_CONTACT_EMAIL") or "").strip() or None
    try:
        view = report.to_email_report()
        _post_report(
            env,
            email_render.build_subject(view),
            _cap_body(email_render.build_text_body(view, contact)),
            transport,
            recipient,
            _cap_html(email_render.build_html_body(view, contact)),
        )
    except (Exception, KeyboardInterrupt) as error:
        _log(f"report send FAILED: {error}")
        return False
    _log("report sent successfully")
    return True


def is_affirmative(answer: str) -> bool:
    """True for y / yes (any casing / surrounding whitespace)."""
    return answer.strip().lower() in {"y", "yes"}


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))


def _load_saved_recipient() -> Optional[str]:
    try:
        with _RECIPIENT_STORE.open("rb") as handle:
            saved = handle.read(400).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return saved if is_valid_email(saved) else None


def _save_recipient(recipient: str) -> None:
    try:
        _RECIPIENT_STORE.write_text(recipient.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


class RecipientOutcome(NamedTuple):
    """Result of recipient resolution."""

    recipient: Optional[str]
    cancelled: bool = False


def configure_recipient(
    *,
    interactive: bool = True,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    max_attempts: int = 3,
) -> RecipientOutcome:
    """Resolve the email recipient."""

    def _ask(prompt: str) -> Optional[str]:
        try:
            return input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    saved = _load_saved_recipient()

    if not interactive:
        return RecipientOutcome(saved)

    for _ in range(max(1, max_attempts)):
        hint = f" [{saved}]" if saved else ""
        entered = _ask(f"Email recipient{hint}: ")
        if entered is None:
            return RecipientOutcome(None, cancelled=True)
        entered = entered.strip()
        if not entered:
            if saved:
                return RecipientOutcome(saved)
            output("Invalid email address - please try again.")
            continue
        if is_valid_email(entered):
            _save_recipient(entered)
            return RecipientOutcome(entered)
        output("Invalid email address - please try again.")

    return RecipientOutcome(None)


def prompt_email_recipient(
    *,
    env: Mapping[str, str],
    interactive: bool = True,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    max_email_attempts: int = 3,
) -> Optional[str]:
    """Prompt for the report email recipient before the suite runs."""

    def _ask(prompt: str) -> Optional[str]:
        try:
            return input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    try:
        if not interactive:
            _log("non-interactive session - report email skipped")
            return None

        missing = _missing_config(env)
        if missing:
            _log(f"report NOT sent - missing configuration: {', '.join(missing)}")
            return None

        answer = _ask("Send the suite report by email when it finishes? [y/N]: ")
        if answer is None:
            _log("report not sent (cancelled)")
            return None
        if not is_affirmative(answer):
            _log("report not sent (declined)")
            return None

        outcome = configure_recipient(
            interactive=True,
            input_fn=input_fn,
            output=output,
            max_attempts=max_email_attempts,
        )
        if outcome.recipient is None:
            if outcome.cancelled:
                _log("report not sent (cancelled)")
            else:
                _log("report not sent - no valid recipient provided")
            return None

        _log(f"the report will be emailed to {outcome.recipient} after the suite finishes")
        return outcome.recipient
    except (Exception, KeyboardInterrupt) as error:
        _log(f"report not sent - unexpected error: {error}")
        return None
