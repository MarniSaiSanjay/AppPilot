"""AppPilot login/onboarding flow compatibility shim."""

from __future__ import annotations

try:
    from ..shared.login import (
        AuthoritativeLoginGoalEvaluator,
        CompositeTerminalEvaluator,
        DEFAULT_GUIDANCE,
        DeterministicTerminalState,
        LLMLoginGoalEvaluator,
        LoginCapability,
        LoginPolicy,
        PROTOTYPE_GOAL,
        SemanticStateEvaluator,
        SemanticTerminalState,
        SharedLoginFlow,
        SignedInCopilotGoalEvaluator,
        build_login_agent,
        main,
        resolve_decision_provider,
        _parse_args,
    )
except ImportError:
    from shared.login import (
        AuthoritativeLoginGoalEvaluator,
        CompositeTerminalEvaluator,
        DEFAULT_GUIDANCE,
        DeterministicTerminalState,
        LLMLoginGoalEvaluator,
        LoginCapability,
        LoginPolicy,
        PROTOTYPE_GOAL,
        SemanticStateEvaluator,
        SemanticTerminalState,
        SharedLoginFlow,
        SignedInCopilotGoalEvaluator,
        build_login_agent,
        main,
        resolve_decision_provider,
        _parse_args,
    )

__all__ = [
    "AuthoritativeLoginGoalEvaluator",
    "CompositeTerminalEvaluator",
    "DEFAULT_GUIDANCE",
    "DeterministicTerminalState",
    "LLMLoginGoalEvaluator",
    "LoginCapability",
    "LoginPolicy",
    "PROTOTYPE_GOAL",
    "SemanticStateEvaluator",
    "SemanticTerminalState",
    "SharedLoginFlow",
    "SignedInCopilotGoalEvaluator",
    "_parse_args",
    "build_login_agent",
    "main",
    "resolve_decision_provider",
]


if __name__ == "__main__":
    raise SystemExit(main())
