"""Deeplink suite CLI."""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Sequence

try:
    from ...apppilot.android import (
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from ...apppilot.agent import (
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from ...apppilot import (
        apk_config,
        apk_trust,
        device_config,
        device_lock,
        email_delivery,
        logtags,
    )
    from ...apppilot import telemetry
    from ...shared.installer import LocalApkInstaller
    from ...shared.account.session import AndroidAccountSession
    from ...shared.credentials import (
        CredentialConfigurationError,
        CredentialProfile,
        CredentialProfileResolver,
    )
    from ...shared.login import (
        ProfiledLoginFlowFactory,
        SharedLoginFlow,
        build_login_agent,
        resolve_decision_provider,
    )
    from ...shared.entra_auth import ensure_entra_model_readiness
    from ...shared.model_client import ModelConfigurationError
except ImportError:
    from apppilot.android import (
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from apppilot.agent import (
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from apppilot import (
        apk_config,
        apk_trust,
        device_config,
        device_lock,
        email_delivery,
        logtags,
    )
    from apppilot import telemetry
    from shared.installer import LocalApkInstaller
    from shared.account.session import AndroidAccountSession
    from shared.credentials import (
        CredentialConfigurationError,
        CredentialProfile,
        CredentialProfileResolver,
    )
    from shared.login import (
        ProfiledLoginFlowFactory,
        SharedLoginFlow,
        build_login_agent,
        resolve_decision_provider,
    )
    from shared.entra_auth import ensure_entra_model_readiness
    from shared.model_client import ModelConfigurationError

from .artifacts import FailureArtifactWriter
from .deeplink_testcase_loader import DeeplinkTestCase, load_deeplink_cases
from .verification import LLMExpectationJudge
from .runner import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    DeeplinkTestRunner,
)
from .orchestrator import DeeplinkSuiteOrchestrator
from .stabilization import build_license_group_stabilizer


_MAX_SELECTION_ATTEMPTS = 5


def _deeplink_testcases_dir() -> Path:
    """Directory owning the Deeplink use case's test workbooks."""
    return Path(__file__).resolve().parents[3] / "testcases" / "deeplinks"


def _discover_workbooks(directory: Path) -> list[Path]:
    """Return selectable Excel workbooks in name order."""
    return sorted(
        (path for path in directory.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.name.casefold(),
    )


def _prompt_for_workbook(
    workbooks: list[Path], *, output, input_fn,
) -> "Path | None":
    """Prompt for a workbook selection."""
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
    """Resolve which workbook to run."""
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


def _resolve_credential_profiles(
    cases: Sequence[DeeplinkTestCase],
    env: "Mapping[str, str] | None" = None,
) -> dict[str, CredentialProfile]:
    """Validate and resolve every workbook License before device operations."""
    return CredentialProfileResolver(env).resolve_all(
        case.license for case in cases
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    if argv is not None:
        argv = list(argv)
        for index, value in enumerate(argv[:-1]):
            if (
                value == "--verify-timeout"
                and argv[index + 1].casefold() in {"nan", "inf", "+inf", "-inf"}
            ):
                argv[index : index + 2] = [
                    f"--verify-timeout={argv[index + 1]}"
                ]
                break
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
    if not math.isfinite(args.verify_timeout) or args.verify_timeout < 0:
        print(
            "ERROR: --verify-timeout must be a finite non-negative number",
            file=sys.stderr,
        )
        return 2

    interactive = getattr(sys.stdin, "isatty", lambda: False)()
    try:
        entra_readiness = ensure_entra_model_readiness(
            env=os.environ,
            interactive=interactive,
            output=lambda message: print(message, file=sys.stderr),
        )
    except ModelConfigurationError as error:
        print(f"ERROR: invalid model configuration: {error}", file=sys.stderr)
        return 2
    if entra_readiness.required and not entra_readiness.ready:
        print(
            f"ERROR: Microsoft Entra model authentication unavailable. "
            f"{entra_readiness.detail}",
            file=sys.stderr,
        )
        return 2

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
    try:
        credential_profiles = _resolve_credential_profiles(cases)
    except CredentialConfigurationError as error:
        print(f"ERROR: invalid credential configuration: {error}", file=sys.stderr)
        return 2
    try:
        trust_config = apk_trust.ApkTrustConfig.from_env()
    except apk_trust.ApkTrustConfigurationError as error:
        print(f"ERROR: invalid APK trust configuration: {error}", file=sys.stderr)
        return 2
    try:
        login_max_actions = _default_max_actions()
        login_max_stuck_actions = _default_max_stuck_actions()
    except ValueError as error:
        print(f"ERROR: invalid runtime configuration: {error}", file=sys.stderr)
        return 2

    apk_path = apk_config.resolve_apk_path(args.apk, interactive=interactive)
    if apk_path is None:
        print("ERROR: no valid APK path provided", file=sys.stderr)
        return 2
    trust_guard = apk_trust.ApkTrustGuard(trust_config)
    try:
        trust_guard.validate_apk(apk_path)
    except apk_trust.ApkTrustError as error:
        print(f"ERROR: APK trust validation failed: {error}", file=sys.stderr)
        return 2

    try:
        judge = LLMExpectationJudge.from_env()
    except ModelConfigurationError as error:
        print(f"ERROR: invalid model configuration: {error}", file=sys.stderr)
        return 2
    if judge is None:
        print(
            "ERROR: no evaluation model configured. Set APPPILOT_MODEL and "
            "configure APPPILOT_MODEL_AUTH as api_key or entra "
            "(plus APPPILOT_MODEL_API_KEY for api_key mode).",
            file=sys.stderr,
        )
        return 2

    device_id = device_config.resolve_device(
        args.device, output=lambda message: print(message, file=sys.stderr),
    )
    if device_id is None:
        return 2
    try:
        observer = MaestroHierarchyObserver(device_id)
        executor = MaestroExecutor(
            trust_config.package_id,
            device_id,
            credential_release_check=lambda timeout: (
                trust_guard.assert_credential_release(
                    device_id, timeout=timeout
                )
            ),
            credential_observer=observer,
        )
    except ValueError as error:
        print(f"ERROR: invalid runtime configuration: {error}", file=sys.stderr)
        return 2
    warm_up = (
        None
        if args.no_warm_up
        else build_license_group_stabilizer(executor)
    )

    try:
        login_provider = resolve_decision_provider()
    except ModelConfigurationError as error:
        print(f"ERROR: invalid model configuration: {error}", file=sys.stderr)
        return 2

    def recover_login_loading() -> None:
        executor.stop_app()
        time.sleep(3.0)
        executor.launch_app()

    login_flows = ProfiledLoginFlowFactory(
        credential_profiles,
        lambda runtime_context: build_login_agent(
            device_id,
            provider=login_provider,
            executor=executor,
            observer=observer,
            foreground_check=executor.is_foreground,
            runtime_context=runtime_context,
            max_actions=login_max_actions,
            max_stuck_actions=login_max_stuck_actions,
        ),
        flow_builder=lambda agent: SharedLoginFlow(
            agent,
            loading_recovery=recover_login_loading,
        ),
    )
    account_session = AndroidAccountSession(
        observer,
        executor,
        lambda profile: login_flows.for_license(profile.license_name),
    )

    logtags.trace(f"using APK: {apk_path}", logtags.INSTALL)
    installer = LocalApkInstaller(
        executor,
        str(apk_path),
        pre_install_check=trust_guard.before_install,
        post_install_check=lambda: trust_guard.verify_installed(device_id),
    )
    run_id = uuid.uuid4().hex
    artifact_writer = FailureArtifactWriter(
        run_id,
        device_id,
        {
            "path": str(apk_path),
            "package_id": trust_config.package_id,
            "certificate_sha256": trust_config.certificate_sha256,
        },
    )
    runner = DeeplinkTestRunner(
        observer=observer,
        executor=executor,
        judge=judge,
        warm_up=warm_up,
        max_attempts=args.max_attempts,
        verify_timeout_seconds=args.verify_timeout,
        login_flow_for_license=login_flows.for_license,
        account_session=account_session,
        profile_for_license=login_flows.profile_for_license,
        installer=installer,
        deterministic_probe=executor,
        expected_package_id=trust_config.package_id,
        artifact_writer=artifact_writer,
    )
    lock = device_lock.DeviceProcessLock(device_id, run_id=run_id)
    try:
        with lock:
            try:
                email_recipient = email_delivery.prompt_email_recipient(
                    env=os.environ, interactive=interactive
                )
            except Exception as error:  # pragma: no cover
                print(
                    logtags.prefix(
                        logtags.EMAIL,
                        f"email prompt failed - continuing without email: {error}",
                    )
                )
                email_recipient = None

            report = DeeplinkSuiteOrchestrator(runner).run(cases)
    except device_lock.DeviceLockError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    telemetry.record_suite_run(report.suite_name, report.total, "Android")
    print(logtags.prefix(logtags.REPORT, "generating report..."))
    report_text = report.format()
    print(report_text)
    print(logtags.prefix(logtags.REPORT, "report generated"))
    if email_recipient is not None:
        print(logtags.prefix(logtags.EMAIL, "triggering email delivery..."))
        try:
            email_delivery.send_suite_report(
                report, env=os.environ, recipient=email_recipient
            )
        except Exception as error:  # pragma: no cover
            print(
                logtags.prefix(
                    logtags.EMAIL,
                    f"report not sent - unexpected error: {error}",
                )
            )
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
