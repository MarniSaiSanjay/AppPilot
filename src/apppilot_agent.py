"""Backward-compatible facade for the AppPilot agent.

The implementation now lives in the ``apppilot`` package and ``flows.login``.
This module re-exports the public names so existing imports (and
``python -m src.apppilot_agent``) keep working unchanged.
"""

from __future__ import annotations

try:  # package-relative (python -m src.apppilot_agent)
    from .apppilot.models import (
        Action,
        ActionKind,
        CredentialKind,
        ExecutionContext,
        GoalEvaluator,
        RuntimeContext,
        UIElement,
        UIObservation,
    )
    from .apppilot.safety import SafetyValidator, infer_credential_kind
    from .apppilot.brain import (
        DecisionRequest,
        LLMModelDecisionProvider,
        ModelDecision,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from .apppilot.android import (
        APP_ID,
        AndroidOperationalError,
        CREDENTIAL_FIELD_ERASE_CHARS,
        MAESTRO_SECRET_ENV,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from .apppilot.agent import (
        AppPilotAgent,
        DEFAULT_MAX_ACTIONS,
        DEFAULT_MAX_STUCK_ACTIONS,
        REPO_ROOT,
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from .flows.login import (
        AuthoritativeLoginGoalEvaluator,
        DEFAULT_GUIDANCE,
        LLMLoginGoalEvaluator,
        PROTOTYPE_GOAL,
        SignedInCopilotGoalEvaluator,
        _parse_args,
        main,
    )
except ImportError:  # top-level (src on sys.path)
    from apppilot.models import (
        Action,
        ActionKind,
        CredentialKind,
        ExecutionContext,
        GoalEvaluator,
        RuntimeContext,
        UIElement,
        UIObservation,
    )
    from apppilot.safety import SafetyValidator, infer_credential_kind
    from apppilot.brain import (
        DecisionRequest,
        LLMModelDecisionProvider,
        ModelDecision,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from apppilot.android import (
        APP_ID,
        AndroidOperationalError,
        CREDENTIAL_FIELD_ERASE_CHARS,
        MAESTRO_SECRET_ENV,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from apppilot.agent import (
        AppPilotAgent,
        DEFAULT_MAX_ACTIONS,
        DEFAULT_MAX_STUCK_ACTIONS,
        REPO_ROOT,
        _default_max_actions,
        _default_max_stuck_actions,
        _load_dotenv,
    )
    from flows.login import (
        AuthoritativeLoginGoalEvaluator,
        DEFAULT_GUIDANCE,
        LLMLoginGoalEvaluator,
        PROTOTYPE_GOAL,
        SignedInCopilotGoalEvaluator,
        _parse_args,
        main,
    )


if __name__ == "__main__":
    raise SystemExit(main())
