"""Backward-compatible facade for the AppPilot agent."""

from __future__ import annotations

try:
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
except ImportError:
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

__all__ = [
    "APP_ID",
    "Action",
    "ActionKind",
    "AndroidOperationalError",
    "AppPilotAgent",
    "AuthoritativeLoginGoalEvaluator",
    "CredentialKind",
    "DEFAULT_GUIDANCE",
    "DEFAULT_MAX_ACTIONS",
    "DEFAULT_MAX_STUCK_ACTIONS",
    "DecisionRequest",
    "ExecutionContext",
    "GoalEvaluator",
    "LLMLoginGoalEvaluator",
    "LLMModelDecisionProvider",
    "MAESTRO_SECRET_ENV",
    "MaestroExecutor",
    "MaestroHierarchyObserver",
    "ModelDecision",
    "ModelDecisionProvider",
    "PROTOTYPE_GOAL",
    "REPO_ROOT",
    "RuntimeContext",
    "SafetyValidator",
    "SignedInCopilotGoalEvaluator",
    "UIElement",
    "UIObservation",
    "UnconfiguredModelDecisionProvider",
    "_default_max_actions",
    "_default_max_stuck_actions",
    "_load_dotenv",
    "_parse_args",
    "infer_credential_kind",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
