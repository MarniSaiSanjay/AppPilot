"""Shared login agent builder and CLI entry point."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Callable

try:
    from ...apppilot.agent import (
        AppPilotAgent,
        DEFAULT_MAX_ACTIONS,
        DEFAULT_MAX_STUCK_ACTIONS,
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from ...apppilot.android import MaestroExecutor, MaestroHierarchyObserver
    from ...apppilot.apk_trust import (
        ApkTrustConfig,
        ApkTrustConfigurationError,
        ApkTrustError,
        ApkTrustGuard,
    )
    from ...apppilot.device_lock import DeviceLockError, DeviceProcessLock
    from ...apppilot.brain import (
        LLMModelDecisionProvider,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from ...apppilot.models import RuntimeContext
    from ...apppilot.safety import SafetyValidator
    from ...apppilot import device_config, logtags
    from ..entra_auth import ensure_entra_model_readiness
    from ..model_client import ModelConfigurationError
except ImportError:
    from apppilot.agent import (
        AppPilotAgent,
        DEFAULT_MAX_ACTIONS,
        DEFAULT_MAX_STUCK_ACTIONS,
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from apppilot.android import MaestroExecutor, MaestroHierarchyObserver
    from apppilot.apk_trust import (
        ApkTrustConfig,
        ApkTrustConfigurationError,
        ApkTrustError,
        ApkTrustGuard,
    )
    from apppilot.device_lock import DeviceLockError, DeviceProcessLock
    from apppilot.brain import (
        LLMModelDecisionProvider,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from apppilot.models import RuntimeContext
    from apppilot.safety import SafetyValidator
    from apppilot import device_config, logtags
    from shared.entra_auth import ensure_entra_model_readiness
    from shared.model_client import ModelConfigurationError

from .goal import (
    AuthoritativeLoginGoalEvaluator,
    DEFAULT_GUIDANCE,
    LLMLoginGoalEvaluator,
    PROTOTYPE_GOAL,
    SemanticStateEvaluator,
    SignedInCopilotGoalEvaluator,
)
from .login_decision_cache import LoginDecisionCache
from .policy import (
    CompositeTerminalEvaluator,
    LoginPolicy,
    SemanticTerminalState,
)


def resolve_decision_provider(
    provider: ModelDecisionProvider | None = None,
) -> ModelDecisionProvider:
    """Resolve the login decision provider."""
    if provider is not None:
        return provider
    provider = LLMModelDecisionProvider.from_env()
    if provider is None:
        provider = UnconfiguredModelDecisionProvider(
            "Set APPPILOT_MODEL and configure APPPILOT_MODEL_AUTH as api_key "
            "or entra (plus APPPILOT_MODEL_API_KEY for api_key mode) to connect "
            "a real decision model."
        )
    return provider


def build_login_agent(
    device: str | None = None,
    *,
    provider: ModelDecisionProvider | None = None,
    observer: MaestroHierarchyObserver | None = None,
    executor: MaestroExecutor | None = None,
    max_actions: int | None = None,
    max_stuck_actions: int | None = None,
    foreground_check: "Callable[[], bool] | None" = None,
    policy: "LoginPolicy | None" = None,
    runtime_context: "RuntimeContext | None" = None,
) -> AppPilotAgent:
    """Build the shared login/onboarding agent."""
    if device is None and (observer is None or executor is None):
        device = device_config.resolve_device(None)
        if device is None:
            raise ValueError("no usable Android device was resolved")

    policy = policy or LoginPolicy.default()
    observer = observer or MaestroHierarchyObserver(device)
    if executor is None:
        trust_guard = ApkTrustGuard(ApkTrustConfig.from_env())
        trust_guard.verify_installed(device)
        executor = MaestroExecutor(
            trust_guard.config.package_id,
            device,
            credential_release_check=lambda timeout: (
                trust_guard.assert_credential_release(
                    device, timeout=timeout
                )
            ),
            credential_observer=observer,
        )
    if isinstance(executor, MaestroExecutor):
        executor.bind_credential_observer(observer)
        if foreground_check is None:
            foreground_check = executor.is_foreground

    deterministic_evaluator = SignedInCopilotGoalEvaluator(
        foreground_check=foreground_check
    )
    semantic_evaluator = LLMLoginGoalEvaluator.from_env(
        foreground_check=foreground_check
    )
    login_completion_terminal = AuthoritativeLoginGoalEvaluator(
        deterministic=deterministic_evaluator,
        semantic=semantic_evaluator,
    )

    shared_semantic = SemanticStateEvaluator.from_env()
    terminals = []
    for terminal in policy.terminals:
        if isinstance(terminal, SemanticTerminalState):
            terminal = terminal.with_evaluator(shared_semantic)
        terminals.append(terminal)
    if policy.stop_at_login_completion:
        terminals.append(login_completion_terminal)

    if terminals == [login_completion_terminal]:
        goal_evaluator = login_completion_terminal
    else:
        goal_evaluator = CompositeTerminalEvaluator(terminals)
    actionable_step_check = getattr(goal_evaluator, "has_actionable_step", None)
    return AppPilotAgent(
        observer=observer,
        goal_evaluator=goal_evaluator,
        decision_provider=LoginDecisionCache(
            resolve_decision_provider(provider)
        ),
        safety_validator=SafetyValidator(),
        executor=executor,
        max_actions=max_actions if max_actions is not None else _default_max_actions(),
        runtime_context=(
            runtime_context
            if runtime_context is not None
            else RuntimeContext.from_env()
        ),
        max_stuck_actions=(
            max_stuck_actions
            if max_stuck_actions is not None
            else _default_max_stuck_actions()
        ),
        nonactionable_wait_seconds=0.5,
        actionable_step_check=actionable_step_check,
        log_tag=logtags.LOGIN,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the AppPilot goal-driven Android agent prototype."
    )
    parser.add_argument(
        "--device",
        help=(
            "Authorized, fully booted ADB device serial. When omitted, reuse "
            "the saved /init device or uniquely auto-detect one."
        ),
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=_default_max_actions(),
        help="Safety bound on actions per run (default: env APPPILOT_MAX_ACTIONS "
        f"or {DEFAULT_MAX_ACTIONS}).",
    )
    parser.add_argument(
        "--max-stuck-actions",
        type=int,
        default=_default_max_stuck_actions(),
        help="Consecutive actions with no meaningful UI change before the run is "
        "stopped early (default: env APPPILOT_MAX_STUCK_ACTIONS or "
        f"{DEFAULT_MAX_STUCK_ACTIONS}).",
    )
    parser.add_argument("--guidance", default=DEFAULT_GUIDANCE)
    return parser.parse_args()


def main() -> int:
    _load_dotenv()

    try:
        args = _parse_args()
    except ValueError as error:
        print(f"ERROR: invalid configuration: {error}", file=sys.stderr)
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
    if args.max_actions < 1:
        print("ERROR: --max-actions must be at least 1", file=sys.stderr)
        return 2
    if args.max_stuck_actions < 1:
        print("ERROR: --max-stuck-actions must be at least 1", file=sys.stderr)
        return 2

    try:
        trust_config = ApkTrustConfig.from_env()
    except ApkTrustConfigurationError as error:
        print(f"ERROR: invalid APK trust configuration: {error}", file=sys.stderr)
        return 2

    device = device_config.resolve_device(args.device)
    if device is None:
        return 2

    trust_guard = ApkTrustGuard(trust_config)
    lock = DeviceProcessLock(device)
    try:
        with lock:
            trust_guard.verify_installed(device)
            observer = MaestroHierarchyObserver(device)
            executor = MaestroExecutor(
                trust_config.package_id,
                device,
                credential_release_check=lambda timeout: (
                    trust_guard.assert_credential_release(
                        device, timeout=timeout
                    )
                ),
                credential_observer=observer,
            )
            agent = build_login_agent(
                device,
                executor=executor,
                observer=observer,
                foreground_check=executor.is_foreground,
                max_actions=args.max_actions,
                max_stuck_actions=args.max_stuck_actions,
            )
            succeeded = False
            try:
                succeeded = agent.run(PROTOTYPE_GOAL, args.guidance)
            finally:
                try:
                    clipboard_cleared = executor.clear_clipboard()
                except (OSError, RuntimeError):
                    clipboard_cleared = False
                if not clipboard_cleared:
                    print(
                        f"{logtags.tag(logtags.LOGIN)} RESULT:\n"
                        "FAIL - clipboard cleanup could not be confirmed"
                    )
                    succeeded = False
            return 0 if succeeded else 1
    except (ValueError, ApkTrustConfigurationError) as error:
        print(f"ERROR: invalid configuration: {error}", file=sys.stderr)
        return 2
    except DeviceLockError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except (
        ApkTrustError,
        OSError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"{logtags.tag(logtags.LOGIN)} RESULT:\nFAIL - {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
