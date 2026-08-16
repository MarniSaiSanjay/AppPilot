"""Shared login agent builder and CLI entry point.

Assembles the generic AppPilotAgent for login/onboarding from a LoginPolicy. No
UI steps are hardcoded: the model decides each action. The policy supplies the
goal/guidance and the ordered terminal states; this builder turns them into the
agent's goal evaluator. By default it reproduces today's login behavior exactly.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Callable

try:  # package-relative (python -m src.shared.login.builder) vs top-level
    from ...apppilot.agent import (
        AppPilotAgent,
        DEFAULT_MAX_ACTIONS,
        DEFAULT_MAX_STUCK_ACTIONS,
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from ...apppilot.android import APP_ID, MaestroExecutor, MaestroHierarchyObserver
    from ...apppilot.brain import (
        LLMModelDecisionProvider,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from ...apppilot.models import RuntimeContext
    from ...apppilot.safety import SafetyValidator
    from ...apppilot import logtags
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.agent import (
        AppPilotAgent,
        DEFAULT_MAX_ACTIONS,
        DEFAULT_MAX_STUCK_ACTIONS,
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from apppilot.android import APP_ID, MaestroExecutor, MaestroHierarchyObserver
    from apppilot.brain import (
        LLMModelDecisionProvider,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from apppilot.models import RuntimeContext
    from apppilot.safety import SafetyValidator
    from apppilot import logtags

from .goal import (
    AuthoritativeLoginGoalEvaluator,
    DEFAULT_GUIDANCE,
    LLMLoginGoalEvaluator,
    PROTOTYPE_GOAL,
    SemanticStateEvaluator,
    SignedInCopilotGoalEvaluator,
)
from .policy import (
    CompositeTerminalEvaluator,
    LoginPolicy,
    SemanticTerminalState,
)


def resolve_decision_provider(
    provider: ModelDecisionProvider | None = None,
) -> ModelDecisionProvider:
    """Return the given provider, else the env-configured LLM provider, else the
    honest Unconfigured placeholder. Centralized so the CLI and other flows
    (e.g. the deeplink runner) share one provider-resolution policy."""
    if provider is not None:
        return provider
    provider = LLMModelDecisionProvider.from_env()
    if provider is None:
        provider = UnconfiguredModelDecisionProvider(
            "Set APPPILOT_MODEL and APPPILOT_MODEL_API_KEY (and optionally "
            "APPPILOT_MODEL_BASE_URL) to connect a real decision model."
        )
    return provider


def build_login_agent(
    device: str = "emulator-5554",
    *,
    provider: ModelDecisionProvider | None = None,
    observer: MaestroHierarchyObserver | None = None,
    executor: MaestroExecutor | None = None,
    max_actions: int | None = None,
    max_stuck_actions: int | None = None,
    foreground_check: "Callable[[], bool] | None" = None,
    policy: "LoginPolicy | None" = None,
) -> AppPilotAgent:
    """Build the single, shared login/onboarding agent (AppPilotAgent + Brain).

    No UI steps are hardcoded: the model decides each action. The policy (default
    ``LoginPolicy.default()``) supplies the ordered terminal states. The built-in
    login-completion terminal - deterministic terminal/blocker evidence first,
    semantic evaluator only for ambiguous screens - is the sensible default and,
    unless the policy opts out, the lowest-priority fallback for custom flows.
    The shared node never encodes use-case-specific meaning; the policy does.
    """
    policy = policy or LoginPolicy.default()

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

    # Use-case semantic terminals author natural language only; the shared node
    # binds the model plumbing (a semantic judge) when they didn't bring one.
    shared_semantic = SemanticStateEvaluator.from_env()
    terminals = []
    for terminal in policy.terminals:
        if isinstance(terminal, SemanticTerminalState):
            terminal = terminal.with_evaluator(shared_semantic)
        terminals.append(terminal)
    if policy.stop_at_login_completion:
        terminals.append(login_completion_terminal)

    # The default login (only the built-in login-completion terminal) uses that
    # evaluator DIRECTLY, so today's agent - object identity, verdicts and
    # actionable-step behavior - is preserved byte-for-byte. Any use case that
    # adds its own terminals gets the ordered composite instead.
    if terminals == [login_completion_terminal]:
        goal_evaluator = login_completion_terminal
    else:
        goal_evaluator = CompositeTerminalEvaluator(terminals)
    # Let the login evaluator classify whether the screen presents a genuine
    # login/onboarding step, so the agent waits/re-observes on transient loading
    # screens (e.g. "looking for accounts" with only a "Terms of use" link)
    # instead of asking the Brain, which would pick a diagnostic Back.
    actionable_step_check = getattr(goal_evaluator, "has_actionable_step", None)
    return AppPilotAgent(
        observer=observer or MaestroHierarchyObserver(device),
        goal_evaluator=goal_evaluator,
        decision_provider=resolve_decision_provider(provider),
        safety_validator=SafetyValidator(),
        executor=executor or MaestroExecutor(APP_ID, device),
        max_actions=max_actions if max_actions is not None else _default_max_actions(),
        runtime_context=RuntimeContext.from_env(),
        max_stuck_actions=(
            max_stuck_actions
            if max_stuck_actions is not None
            else _default_max_stuck_actions()
        ),
        actionable_step_check=actionable_step_check,
        log_tag=logtags.LOGIN,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the AppPilot goal-driven Android agent prototype."
    )
    parser.add_argument("--device", default="emulator-5554")
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
    # Load the local, git-ignored .env first so values such as the model
    # credentials and APPPILOT_MAX_ACTIONS are available before args are parsed.
    # Real environment variables always take precedence.
    _load_dotenv()

    args = _parse_args()
    if args.max_actions < 1:
        print("ERROR: --max-actions must be at least 1", file=sys.stderr)
        return 2
    if args.max_stuck_actions < 1:
        print("ERROR: --max-stuck-actions must be at least 1", file=sys.stderr)
        return 2

    agent = build_login_agent(
        args.device,
        max_actions=args.max_actions,
        max_stuck_actions=args.max_stuck_actions,
    )
    try:
        return 0 if agent.run(PROTOTYPE_GOAL, args.guidance) else 1
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"{logtags.tag(logtags.LOGIN)} RESULT:\nFAIL - {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
