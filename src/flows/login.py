"""AppPilot login/onboarding flow (compatibility shim).

The login implementation now lives in the shared, use-case-agnostic node
``shared.login``. This module re-exports the same public surface so existing
imports (``flows.login`` and the ``apppilot_agent`` facade) and
``python -m flows.login`` keep working unchanged.
"""

from __future__ import annotations

try:  # package-relative (python -m src.flows.login) vs top-level (src on path)
    from ..shared.login import (  # noqa: F401
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
except ImportError:  # top-level (src on sys.path)
    from shared.login import (  # noqa: F401
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


if __name__ == "__main__":
    raise SystemExit(main())
