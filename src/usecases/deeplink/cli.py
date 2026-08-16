"""Deeplink suite CLI entry point (use-case-specific).

Parses the deeplink flags, loads the Excel test cases, wires the semantic judge,
the Maestro executor/observer, the shared login/installer/warm-up nodes and the
local officemobile APK build, then runs the suite via the orchestrator and
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
    from ...apppilot import email_report, logtags, officemobile_build
    from ...shared.warmup import MaestroWarmUp
    from ...shared.installer import LocalApkInstaller
    from ...shared.login import LoginCapability, SharedLoginFlow, build_login_agent
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.android import APP_ID, MaestroExecutor, MaestroHierarchyObserver
    from apppilot.agent import _load_dotenv
    from apppilot import email_report, logtags, officemobile_build
    from shared.warmup import MaestroWarmUp
    from shared.installer import LocalApkInstaller
    from shared.login import LoginCapability, SharedLoginFlow, build_login_agent

from .testcases import load_deeplink_cases
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
def _default_excel_path() -> Path:
    return Path(__file__).resolve().parents[3] / "testcases" / "deeplinks" / (
        "deeplink_tests.xlsx"
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the data-driven AppPilot deeplink test suite."
    )
    parser.add_argument(
        "--excel",
        default=str(_default_excel_path()),
        help="Path to the deeplink test-case Excel (default: bundled workbook).",
    )
    parser.add_argument("--device", default="emulator-5554")
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
        "--rebuild", action="store_true",
        help="Force a fresh officemobile build even if a prior APK exists.",
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

    try:
        cases = load_deeplink_cases(args.excel)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"ERROR: could not load deeplink test cases: {error}", file=sys.stderr)
        return 2

    judge = LLMExpectationJudge.from_env()
    if judge is None:
        print(
            "ERROR: no evaluation model configured. Set APPPILOT_MODEL and "
            "APPPILOT_MODEL_API_KEY (and optionally APPPILOT_MODEL_BASE_URL).",
            file=sys.stderr,
        )
        return 2

    executor = MaestroExecutor(APP_ID, args.device)
    observer = MaestroHierarchyObserver(args.device)
    warm_up = None if args.no_warm_up else MaestroWarmUp(executor)

    # Shared login capability: the same generic AppPilotAgent + Brain, reusing
    # the device's executor/observer (DRY). The executor's foreground check is
    # injected so login never completes while the app is not foreground.
    login_agent = build_login_agent(
        args.device,
        executor=executor,
        observer=observer,
        foreground_check=executor.is_foreground,
    )
    login_flow: LoginCapability = SharedLoginFlow(login_agent)

    # Build (or reuse) the local officemobile APK and install it via adb - never
    # from the actual Play Store.
    try:
        apk_path = officemobile_build.APK_PATH
        if args.rebuild or not apk_path.exists():
            logtags.trace("building local officemobile APK", logtags.BUILD)
            apk_path = officemobile_build.build_apk()
        logtags.trace(f"using local APK: {apk_path}", logtags.BUILD)
    except officemobile_build.BuildError as error:
        print(f"ERROR: could not build local APK: {error}", file=sys.stderr)
        return 2
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
        interactive = getattr(sys.stdin, "isatty", lambda: False)()
        email_recipient = email_report.prompt_email_recipient(
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
    print(logtags.prefix(logtags.REPORT, "generating report..."))
    report_text = report.format()
    print(report_text)
    print(logtags.prefix(logtags.REPORT, "report generated"))
    # TESTS -> FINAL REPORT -> EMAIL. Deliver to the address chosen up front (if
    # any). Delivery must never affect the verdict, so failure is swallowed.
    if email_recipient is not None:
        print(logtags.prefix(logtags.EMAIL, "triggering email delivery..."))
        try:
            email_report.send_suite_report(
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
