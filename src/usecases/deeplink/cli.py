"""Deeplink suite CLI entry point (use-case-specific).

Parses the deeplink flags, loads the Excel test cases, wires the semantic judge,
the Maestro executor/observer, the shared login/installer/warm-up nodes and the
user-provided local APK path, then runs the suite via the orchestrator and
optionally emails the report. Behavior, flags, exit codes and output are the
Deeplink use case's own contract.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path
from typing import Sequence

try:  # package-relative (python -m src.usecases.deeplink.cli) vs top-level
    from ...apppilot.android import APP_ID, MaestroExecutor, MaestroHierarchyObserver
    from ...apppilot.agent import _load_dotenv
    from ...apppilot import apk_config, device_config, email_delivery, logtags
    from ...apppilot import telemetry
    from ...shared.warmup import MaestroWarmUp
    from ...shared.installer import LocalApkInstaller
    from ...shared.login import LoginCapability, SharedLoginFlow, build_login_agent
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.android import APP_ID, MaestroExecutor, MaestroHierarchyObserver
    from apppilot.agent import _load_dotenv
    from apppilot import apk_config, device_config, email_delivery, logtags
    from apppilot import telemetry
    from shared.warmup import MaestroWarmUp
    from shared.installer import LocalApkInstaller
    from shared.login import LoginCapability, SharedLoginFlow, build_login_agent

from .deeplink_testcase_loader import load_deeplink_cases
from .verification import LLMExpectationJudge
from .runner import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    DeeplinkTestRunner,
)
from .orchestrator import DeeplinkSuiteOrchestrator


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
# Bound the interactive suite-selection reprompt so bad input can never loop
# forever (the run fails cleanly instead).
_MAX_SELECTION_ATTEMPTS = 5


def _deeplink_testcases_dir() -> Path:
    """Directory owning the Deeplink use case's test workbooks."""
    return Path(__file__).resolve().parents[3] / "testcases" / "deeplinks"


def _discover_workbooks(directory: Path) -> list[Path]:
    """Return the selectable ``.xlsx`` suites in ``directory`` (sorted by name),
    ignoring temporary Excel lock files (``~$*.xlsx``)."""
    return sorted(
        (path for path in directory.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.name.casefold(),
    )


def _prompt_for_workbook(
    workbooks: list[Path], *, output, input_fn,
) -> "Path | None":
    """Show a numbered suite list and return the chosen workbook. Empty input
    picks the first ([1]); invalid input reprompts. Returns None if no valid
    choice is made (EOF / too many invalid entries) so the caller fails cleanly."""
    output("Available test suites:")
    for index, workbook in enumerate(workbooks, start=1):
        output(f"{index}. {workbook.name}")
    for _ in range(_MAX_SELECTION_ATTEMPTS):
        try:
            raw = input_fn("Select test suite [1]: ").strip()
        except EOFError:
            return workbooks[0]
        if not raw:
            return workbooks[0]
        if raw.isdigit() and 1 <= int(raw) <= len(workbooks):
            return workbooks[int(raw) - 1]
        output(f"Invalid selection {raw!r}; enter a number 1-{len(workbooks)}.")
    return None


def _select_workbook(
    explicit: "str | None",
    *,
    directory: Path,
    interactive: bool,
    output,
    input_fn=input,
) -> "Path | None":
    """Resolve which workbook to run.

    * An explicit ``--excel`` path always wins and skips discovery/prompting.
    * Otherwise discover suites in ``directory``: none -> clean failure (None);
      exactly one -> auto-select; several -> prompt when interactive, else fail
      cleanly asking for an explicit ``--excel`` (automation contract).
    """
    if explicit is not None:
        return Path(explicit)
    workbooks = _discover_workbooks(directory)
    if not workbooks:
        output(f"ERROR: no test suites (.xlsx) found in {directory}")
        return None
    if len(workbooks) == 1:
        output(f"Found 1 test suite: {workbooks[0].name}")
        output(f"Running suite: {workbooks[0].name}")
        return workbooks[0]
    if not interactive:
        output(
            "ERROR: multiple test suites found; pass --excel PATH to choose one "
            "in non-interactive mode"
        )
        return None
    return _prompt_for_workbook(workbooks, output=output, input_fn=input_fn)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the data-driven AppPilot deeplink test suite."
    )
    parser.add_argument(
        "--excel",
        default=None,
        help=(
            "Path to a specific deeplink test-case Excel. If omitted, the "
            "workbooks in the Deeplink testcases directory are discovered and "
            "(when more than one) you are prompted to select a suite."
        ),
    )
    parser.add_argument(
        "--device", default=None,
        help=(
            "ADB device serial to target. If omitted, the device selected during "
            "/init is reused (when still connected), otherwise a single connected "
            "device is auto-detected."
        ),
    )
    parser.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
        help="Maximum attempts per deeplink test case (default 2: 1 try + 1 retry).",
    )
    parser.add_argument(
        "--verify-timeout", type=float, default=DEFAULT_VERIFY_TIMEOUT_SECONDS,
        help=(
            "Maximum seconds to poll for the expected result after a deeplink "
            "before declaring a mismatch (default 30)."
        ),
    )
    parser.add_argument(
        "--no-warm-up", action="store_true",
        help="Skip the one-time first-install warm-up before the suite.",
    )
    parser.add_argument(
        "--apk", default=None,
        help=(
            "Path to the local .apk to install. If omitted, a previously saved "
            "path is offered/reused, otherwise you are prompted for one."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv()
    args = _parse_args(argv)
    if args.max_attempts < 1:
        print("ERROR: --max-attempts must be at least 1", file=sys.stderr)
        return 2
    if args.verify_timeout < 0:
        print("ERROR: --verify-timeout must not be negative", file=sys.stderr)
        return 2

    interactive = getattr(sys.stdin, "isatty", lambda: False)()
    # Test-suite selection (runtime CLI concern; never persisted, never in /init).
    # --excel is an explicit override that skips discovery/prompting; otherwise
    # discover the Deeplink workbooks and select (auto when only one).
    excel_path = _select_workbook(
        args.excel,
        directory=_deeplink_testcases_dir(),
        interactive=interactive,
        output=lambda message: print(message, file=sys.stderr),
    )
    if excel_path is None:
        return 2

    try:
        cases = load_deeplink_cases(excel_path)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"ERROR: could not load deeplink test cases: {error}", file=sys.stderr)
        return 2

    # AppPilot installs ONLY a user-provided local APK (no build/acquisition).
    # Resolve it up front from --apk, a saved path, or an interactive prompt, and
    # validate before any execution. An unresolved/invalid path is a clean setup
    # error (exit 2); it never reaches test execution and never leaks a traceback.
    apk_path = apk_config.resolve_apk_path(args.apk, interactive=interactive)
    if apk_path is None:
        print("ERROR: no valid APK path provided", file=sys.stderr)
        return 2

    judge = LLMExpectationJudge.from_env()
    if judge is None:
        print(
            "ERROR: no evaluation model configured. Set APPPILOT_MODEL and "
            "APPPILOT_MODEL_API_KEY (and optionally APPPILOT_MODEL_BASE_URL).",
            file=sys.stderr,
        )
        return 2

    # Resolve the target device: explicit --device wins; otherwise reuse the
    # /init selection (revalidated live) or auto-detect a single connected device
    # (device.py owns detection). Ambiguous multi-device with no choice exits 2.
    device_id = device_config.resolve_device(
        args.device, output=lambda message: print(message, file=sys.stderr),
    )
    if device_id is None:
        return 2

    executor = MaestroExecutor(APP_ID, device_id)
    observer = MaestroHierarchyObserver(device_id)
    warm_up = None if args.no_warm_up else MaestroWarmUp(executor)

    # Shared login capability: the same generic AppPilotAgent + Brain, reusing
    # the device's executor/observer (DRY). The executor's foreground check is
    # injected so login never completes while the app is not foreground.
    login_agent = build_login_agent(
        device_id,
        executor=executor,
        observer=observer,
        foreground_check=executor.is_foreground,
    )
    login_flow: LoginCapability = SharedLoginFlow(login_agent)

    logtags.trace(f"using APK: {apk_path}", logtags.INSTALL)
    installer = LocalApkInstaller(executor, str(apk_path))
    runner = DeeplinkTestRunner(
        observer=observer,
        executor=executor,
        judge=judge,
        warm_up=warm_up,
        max_attempts=args.max_attempts,
        verify_timeout_seconds=args.verify_timeout,
        login_flow=login_flow,
        installer=installer,
    )
    # Ask UP FRONT whether to email the report and to whom, so the operator can
    # start the (long) suite and walk away; delivery happens automatically once
    # the run finishes. Prompting is isolated and must never affect the suite
    # result, so any failure here is swallowed.
    try:
        email_recipient = email_delivery.prompt_email_recipient(
            env=os.environ, interactive=interactive
        )
    except Exception as error:  # pragma: no cover - defensive; prompt never raises
        print(
            logtags.prefix(
                logtags.EMAIL,
                f"email prompt failed - continuing without email: {error}",
            )
        )
        email_recipient = None

    # The orchestrator owns the explicit top-level lifecycle and composes the
    # runner for per-case execution, judging, retry and reporting.
    report = DeeplinkSuiteOrchestrator(runner).run(cases)
    # Best-effort telemetry (roadmap #8/#9): one minimal record per real suite
    # run, submitted from the CLI seam only (never in the orchestrator, so direct
    # orchestrator/test calls emit nothing). Never raises; never affects results.
    telemetry.record_suite_run(report.suite_name, report.total, "Android")
    print(logtags.prefix(logtags.REPORT, "generating report..."))
    report_text = report.format()
    print(report_text)
    print(logtags.prefix(logtags.REPORT, "report generated"))
    # TESTS -> FINAL REPORT -> EMAIL. Deliver to the address chosen up front (if
    # any). Delivery must never affect the verdict, so failure is swallowed.
    if email_recipient is not None:
        print(logtags.prefix(logtags.EMAIL, "triggering email delivery..."))
        try:
            email_delivery.send_suite_report(
                report, env=os.environ, recipient=email_recipient
            )
        except Exception as error:  # pragma: no cover - defensive last line
            print(
                logtags.prefix(
                    logtags.EMAIL,
                    f"report not sent - unexpected error: {error}",
                )
            )
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
