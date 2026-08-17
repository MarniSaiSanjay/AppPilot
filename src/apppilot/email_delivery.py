"""Send a test-suite report by email via the AppPilot email relay.

Isolated, use-case-agnostic delivery layer. The report is delivered by a single
authenticated HTTPS POST to the AppPilot email relay service, which owns the
fixed sender (and a default recipient) and holds the privileged Azure
Communication Services credential server-side - so no email/Azure credential
ever lives in this CLI. Stdlib-only (urllib). The suite report stays the source
of truth; this module only delivers it (presentation lives in
:mod:`apppilot.email_render`). Email failures are logged under ``[EMAIL]`` and
never change the suite result.

Before the suite runs the CLI asks the operator whether to send the report and,
if so, for the recipient address (see :func:`prompt_email_recipient`), so a long
run can be started unattended and the report emailed automatically when it ends
(via :func:`send_suite_report`). The relay still owns the fixed, service-owned
sender and the privileged Azure credential; only the recipient is chosen at run
time.

Configuration (from the environment / .env; the end-user supplies nothing):
  APPPILOT_EMAIL_API_URL   the relay ``/send-report`` endpoint
  APPPILOT_EMAIL_API_KEY   the scoped relay API key
  APPPILOT_CONTACT_EMAIL   optional footer contact address (omitted if unset)
"""

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

# Pragmatic address check - the relay/ACS is the real authority on deliverability.
# \Z (not $) so a trailing newline can never sneak into the address.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")

# Last recipient is remembered here (gitignored) so it can be offered as a default.
_RECIPIENT_STORE = Path(__file__).resolve().parents[2] / ".apppilot_recipient"

# The emailed body is capped so a huge suite can never trip the relay's size limit
# (relay rejects bodies over 200_000 chars); leaves headroom for the summary header.
_MAX_BODY_CHARS = 190_000

# The relay is scale-to-zero, so the first request after an idle period pays a
# cold-start penalty (~30-60s) before it responds. The timeout must comfortably
# exceed that, or a healthy send is aborted mid-flight (seen as "read operation
# timed out"). Delivery failure never affects the suite result, only whether the
# email arrives - so err on the side of waiting.
_HTTP_TIMEOUT_SECONDS = 120

# transport(request) -> (status_code, response_text); injectable for tests.
Transport = Callable[[urllib.request.Request], "tuple[int, str]"]


def _log(message: str) -> None:
    print(logtags.prefix(logtags.EMAIL, message))


def _missing_config(env: Mapping[str, str]) -> "list[str]":
    return [key for key in _REQUIRED_ENV if not env.get(key)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse relay redirects so the API key can never be replayed to another
    host or downgraded to http. A 3xx from the relay is never legitimate."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _default_transport(request: urllib.request.Request) -> "tuple[int, str]":
    with _OPENER.open(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return response.status, response.read().decode("utf-8")


def _cap_body(body: str) -> str:
    """Cap the plain-text body so a huge suite can never trip the relay's size
    limit. The console output remains the complete record."""
    if len(body) > _MAX_BODY_CHARS:
        return (
            body[:_MAX_BODY_CHARS]
            + "\n\n[... report truncated for email; see console output ...]"
        )
    return body


def _cap_html(html_body: str) -> str:
    """Drop the HTML entirely if it would exceed the relay limit rather than
    sending a truncated/broken table; the plain-text body (always sent
    alongside) stays the complete record."""
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
    """Email the final suite report through the relay. When ``recipient`` is
    given it is delivered there; otherwise the relay's fixed recipient is used.
    Returns True on success, False on any failure (missing config, relay/network
    error). Never raises - the suite result is independent of email delivery, so
    any error (network timeout, TLS, malformed report, buggy transport, Ctrl-C)
    is swallowed and reported as a failed send."""
    missing = _missing_config(env)
    if missing:
        _log(f"report NOT sent - missing configuration: {', '.join(missing)}")
        return False
    destination = recipient or "the configured default recipient"
    # Emitted before the POST so the (possibly long) cold-start wait isn't silent.
    _log(f"sending report to {destination} - this can take up to a minute...")
    contact = (env.get("APPPILOT_CONTACT_EMAIL") or "").strip() or None
    try:
        # The report maps itself onto the generic email view; presentation is
        # rendered by email_render, which has no use-case knowledge.
        view = report.to_email_report()
        _post_report(
            env,
            email_render.build_subject(view),
            _cap_body(email_render.build_text_body(view, contact)),
            transport,
            recipient,
            _cap_html(email_render.build_html_body(view, contact)),
        )
    except (Exception, KeyboardInterrupt) as error:  # never affect the suite result
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
        # Read bounded bytes so a corrupt/oversized store can never raise or
        # blow up memory; errors="replace" neutralizes non-UTF-8 content.
        with _RECIPIENT_STORE.open("rb") as handle:
            saved = handle.read(400).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return saved if is_valid_email(saved) else None


def _save_recipient(recipient: str) -> None:
    try:
        _RECIPIENT_STORE.write_text(recipient.strip() + "\n", encoding="utf-8")
    except OSError:
        pass  # Persisting the default is best-effort; never block the send.


class RecipientOutcome(NamedTuple):
    """Result of :func:`configure_recipient`.

    ``recipient`` is the resolved address, or ``None`` when none was chosen.
    ``cancelled`` distinguishes an EOF/Ctrl-C abort from exhausting the attempts,
    so callers can log/report the two cases differently.
    """

    recipient: Optional[str]
    cancelled: bool = False


def configure_recipient(
    *,
    interactive: bool = True,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    max_attempts: int = 3,
) -> RecipientOutcome:
    """Resolve the email recipient via the shared bracketed-default prompt.

    Single source of truth for recipient configuration, reused by BOTH the
    normal-run send flow (:func:`prompt_email_recipient`) and ``/init``. It owns:
    saved-recipient loading, address validation, the ``Email recipient
    [<saved>]:`` prompt, Enter-keeps-the-saved-default, replace/persist of a new
    valid address, bounded invalid-input reprompting, never overwriting the saved
    default with an invalid entry, and clean EOF/Ctrl-C cancellation.

    Never sends and never raises. Non-interactive runs reuse a saved, still-valid
    recipient (if any) WITHOUT prompting. The returned :class:`RecipientOutcome`
    reports whether resolution was cancelled so callers can distinguish an abort
    from exhausted attempts.
    """

    def _ask(prompt: str) -> Optional[str]:
        # Ctrl-C / EOF signals cancellation (None), kept distinct from an empty
        # answer ("") so aborting can never be mistaken for "accept the default".
        try:
            return input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    saved = _load_saved_recipient()

    # Non-interactive: reuse a saved recipient only; never prompt.
    if not interactive:
        return RecipientOutcome(saved)

    # Interactive: a single bracketed-default prompt (no Yes/No step). Enter keeps
    # the saved default unchanged; typing a valid address replaces it.
    for _ in range(max(1, max_attempts)):
        hint = f" [{saved}]" if saved else ""
        entered = _ask(f"Email recipient{hint}: ")
        if entered is None:  # Ctrl-C / EOF: abort, never reuse the saved default.
            return RecipientOutcome(None, cancelled=True)
        entered = entered.strip()
        if not entered:
            if saved:  # Enter keeps the saved default; never re-saved/overwritten.
                return RecipientOutcome(saved)
            output("Invalid email address - please try again.")
            continue
        if is_valid_email(entered):
            _save_recipient(entered)  # replace the saved default with the new one
            return RecipientOutcome(entered)
        output("Invalid email address - please try again.")  # never overwrites saved

    return RecipientOutcome(None)


def prompt_email_recipient(
    *,
    env: Mapping[str, str],
    interactive: bool = True,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    max_email_attempts: int = 3,
) -> Optional[str]:
    """Ask UP FRONT (before the suite runs) whether to email the report and, if
    so, to which address - so the operator can start a long run and walk away.

    Returns the recipient address to deliver to once the suite finishes, or
    None to send nothing (non-interactive, missing config, declined, cancelled,
    or no valid address). The chosen address is persisted as the next default.
    Never raises - email must never affect the suite verdict or exit status.

    The recipient prompt itself is owned by the shared :func:`configure_recipient`
    (same bracketed-default UX used by ``/init``); this wrapper adds only the
    per-run "Send report?" decision and the relay-config fail-fast."""

    def _ask(prompt: str) -> Optional[str]:
        # Ctrl-C / EOF signals cancellation (None), kept distinct from an empty
        # answer ("") so aborting can never be mistaken for "accept the default".
        try:
            return input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    try:
        if not interactive:
            _log("non-interactive session - report email skipped")
            return None

        # Fail fast (before asking anything) if email isn't configured.
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

        # Recipient resolution/persistence is delegated to the shared helper.
        outcome = configure_recipient(
            interactive=True,
            input_fn=input_fn,
            output=output,
            max_attempts=max_email_attempts,
        )
        if outcome.recipient is None:
            if outcome.cancelled:  # Ctrl-C / EOF: abort, never send to the default.
                _log("report not sent (cancelled)")
            else:
                _log("report not sent - no valid recipient provided")
            return None

        _log(f"the report will be emailed to {outcome.recipient} after the suite finishes")
        return outcome.recipient
    except (Exception, KeyboardInterrupt) as error:  # never affect the suite result
        _log(f"report not sent - unexpected error: {error}")
        return None
