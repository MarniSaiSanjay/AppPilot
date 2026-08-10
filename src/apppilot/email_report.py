"""Send the final deeplink suite report by email via the AppPilot email relay.

Isolated component. The report is delivered by a single authenticated HTTPS POST
to the AppPilot email relay service, which owns the fixed sender (and a default
recipient) and holds the privileged Azure Communication Services credential
server-side - so no email/Azure credential ever lives in this CLI. Stdlib-only
(urllib). The suite report stays the source of truth; this module only
summarizes and delivers it. Email failures are logged under ``[EMAIL]`` and
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
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

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
    print(f"[EMAIL] {message}")


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


def build_subject(report) -> str:
    result = "PASS" if report.failed == 0 else "FAIL"
    return f"AppPilot Deeplink Suite: {result} ({report.passed}/{report.total} passed)"


def build_body(report) -> str:
    """Summary header (suite result + installed/uninstalled breakdown) followed
    by the existing report text, which stays the source of truth."""
    result = "PASS" if report.failed == 0 else "FAIL"
    installed = [r for r in report.results if r.case.installed]
    uninstalled = [r for r in report.results if not r.case.installed]

    def _passed(results: Sequence) -> int:
        return sum(1 for r in results if r.passed)

    header = [
        f"Suite result: {result}",
        f"Total cases: {report.total}",
        f"Passed: {report.passed}   Failed: {report.failed}",
        f"Installed: {len(installed)} "
        f"({_passed(installed)} passed, {len(installed) - _passed(installed)} failed)",
        f"Uninstalled: {len(uninstalled)} "
        f"({_passed(uninstalled)} passed, {len(uninstalled) - _passed(uninstalled)} failed)",
        "",
        "-" * 60,
        "",
    ]
    body = "\n".join(header) + report.format()
    if len(body) > _MAX_BODY_CHARS:
        body = (
            body[:_MAX_BODY_CHARS]
            + "\n\n[... report truncated for email; see console output ...]"
        )
    return body


# Inline styles only (email clients strip <style>/external CSS). PASS/FAIL use a
# solid green/red background with white text, as requested.
_TH_STYLE = (
    "background:#1f4e79;color:#ffffff;border:1px solid #244;"
    "padding:8px 12px;text-align:left;font-weight:bold;"
)
_TD_STYLE = "border:1px solid #244;padding:8px 12px;"
_PASS_STYLE = _TD_STYLE + "background:#1e7e34;color:#ffffff;font-weight:bold;text-align:center;"
_FAIL_STYLE = _TD_STYLE + "background:#c62828;color:#ffffff;font-weight:bold;text-align:center;"

_HTML_COLUMNS = ("S.No", "Test ID", "User", "Installed", "Attempt", "Expected", "Result")

_ATTEMPT_MATCH_COLOR = "#1e7e34"
_ATTEMPT_MISMATCH_COLOR = "#c62828"

_TESTID_CHUNK_RE = re.compile(r"(\d+)")


def _testid_sort_key(test_id: str):
    """Natural sort key for a Test ID so e.g. TC2 sorts before TC10.

    Splits into alternating text/number chunks; numbers compare numerically,
    text case-insensitively. Purely deterministic (no locale dependence)."""
    chunks = _TESTID_CHUNK_RE.split(str(test_id))
    return [
        (1, int(chunk)) if chunk.isdigit() else (0, chunk.lower())
        for chunk in chunks
        if chunk != ""
    ]


def build_html_body(report) -> str:
    """Render the suite report as a bordered HTML table for the email.

    Same data as the plain-text report, laid out as a table with a styled header
    and a Result column showing PASS on a green background / FAIL on a red one.
    Cases are ordered by Test ID. A "Details" section after the table lists, per
    case, the deeplink/overall verdicts, the expected result, and every attempt's
    match/mismatch reason (the same per-attempt explanations from the plain-text
    report). Inline styles only, so it renders in clients that strip <style>.
    """
    result = "PASS" if report.failed == 0 else "FAIL"
    installed = [r for r in report.results if r.case.installed]
    uninstalled = [r for r in report.results if not r.case.installed]

    def _passed(results: Sequence) -> int:
        return sum(1 for r in results if r.passed)

    def _esc(value) -> str:
        return html.escape(str(value))

    # Order cases by Test ID (natural sort so TC2 precedes TC10).
    ordered = sorted(report.results, key=lambda r: _testid_sort_key(r.case.test_id))

    rows = []
    for index, r in enumerate(ordered, start=1):
        passed = r.passed
        attempt = r.passing_attempt or len(r.attempts)
        status = "PASS" if passed else "FAIL"
        status_style = _PASS_STYLE if passed else _FAIL_STYLE
        cells = [
            f'<td style="{_TD_STYLE}">{index}</td>',
            f'<td style="{_TD_STYLE}">{_esc(r.case.test_id)}</td>',
            f'<td style="{_TD_STYLE}">{_esc(r.case.user_type or "-")}</td>',
            f'<td style="{_TD_STYLE}">{"yes" if r.case.installed else "no"}</td>',
            f'<td style="{_TD_STYLE}">{attempt}</td>',
            f'<td style="{_TD_STYLE}">{_esc(r.case.expected_result)}</td>',
            f'<td style="{status_style}">{status}</td>',
        ]
        rows.append("<tr>" + "".join(cells) + "</tr>")

    headers = "".join(f'<th style="{_TH_STYLE}">{col}</th>' for col in _HTML_COLUMNS)
    suite_color = "#1e7e34" if report.failed == 0 else "#c62828"

    # Per-case explanation blocks, listed AFTER the table (not inside it) so the
    # grid stays clean. Same verdicts + attempt reasons as the plain-text report.
    detail_blocks = []
    for r in ordered:
        passed = r.passed
        attempt = r.passing_attempt or len(r.attempts)
        status = "PASS" if passed else "FAIL"
        header_color = _ATTEMPT_MATCH_COLOR if passed else _ATTEMPT_MISMATCH_COLOR
        block = [
            f'<div style="margin:14px 0 4px;">'
            f'<b>{_esc(r.case.test_id)}</b> '
            f'<b style="color:{header_color};">{status}</b> '
            f'&nbsp; Attempt {attempt}</div>',
            f'<div style="margin:0 0 2px;color:#3c4043;">'
            f'Deeplink test: <b>{status}</b> &nbsp;|&nbsp; '
            f'Overall test case: <b>{status}</b></div>',
            f'<div style="margin:0 0 4px;color:#3c4043;">'
            f'Expected: {_esc(r.case.expected_result)}</div>',
        ]
        for a in r.attempts:
            mark = "match" if a.matched else "mismatch"
            color = _ATTEMPT_MATCH_COLOR if a.matched else _ATTEMPT_MISMATCH_COLOR
            block.append(
                f'<div style="margin:0 0 3px 16px;color:#3c4043;">'
                f'Attempt {a.attempt}: '
                f'<b style="color:{color};">{mark}</b> - {_esc(a.reason)}</div>'
            )
        detail_blocks.append("".join(block))

    parts = [
        "<html><body style=\"font-family:'Segoe UI',Arial,sans-serif;color:#202124;\">",
        "<h2 style=\"margin:0 0 8px;\">AppPilot Deeplink Suite Report</h2>",
        f'<p style="margin:0 0 4px;">Suite result: '
        f'<b style="color:{suite_color};">{result}</b> '
        f"&mdash; {report.passed}/{report.total} passed</p>",
        f'<p style="margin:0 0 12px;color:#5f6368;">'
        f"Installed: {len(installed)} ({_passed(installed)} passed, "
        f"{len(installed) - _passed(installed)} failed) &nbsp;|&nbsp; "
        f"Uninstalled: {len(uninstalled)} ({_passed(uninstalled)} passed, "
        f"{len(uninstalled) - _passed(uninstalled)} failed)</p>",
        '<table style="border-collapse:collapse;font-size:14px;">',
        f"<thead><tr>{headers}</tr></thead>",
        "<tbody>" + "".join(rows) + "</tbody>",
        "</table>",
        '<h3 style="margin:20px 0 4px;">Details</h3>',
        "".join(detail_blocks),
        "</body></html>",
    ]
    body = "".join(parts)
    if len(body) > _MAX_BODY_CHARS:
        # Fall back to no HTML rather than sending a truncated/broken table; the
        # plain-text body (always sent alongside) remains the complete record.
        return ""
    return body


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
    try:
        _post_report(
            env,
            build_subject(report),
            build_body(report),
            transport,
            recipient,
            build_html_body(report),
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
    Never raises - email must never affect the suite verdict or exit status."""

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

        saved = _load_saved_recipient()
        recipient = None
        for _ in range(max(1, max_email_attempts)):
            hint = f" [{saved}]" if saved else ""
            entered = _ask(f"Recipient email address{hint}: ")
            if entered is None:  # Ctrl-C / EOF: abort, never send to the default.
                _log("report not sent (cancelled)")
                return None
            entered = entered.strip()
            if not entered and saved:
                recipient = saved
                break
            if is_valid_email(entered):
                recipient = entered
                break
            output("Invalid email address - please try again.")
        if not recipient:
            _log("report not sent - no valid recipient provided")
            return None

        _save_recipient(recipient)
        _log(f"the report will be emailed to {recipient} after the suite finishes")
        return recipient
    except (Exception, KeyboardInterrupt) as error:  # never affect the suite result
        _log(f"report not sent - unexpected error: {error}")
        return None
