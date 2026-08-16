"""Shared, use-case-agnostic login node.

A generic AppPilotAgent-based login/onboarding capability that any use case can
drive. The use case supplies WHAT/WHY through a ``LoginPolicy`` (natural-language
goal/guidance and ordered terminal states); this node owns HOW (driving sign-in
and stopping at a terminal before offering any action). Nothing here encodes
Deeplink-, FRI-, or any other use-case-specific branching.

Public surface (import from here or via the ``flows.login`` compatibility shim):
  * Goal evaluators: SignedInCopilotGoalEvaluator, LLMLoginGoalEvaluator,
    AuthoritativeLoginGoalEvaluator, SemanticStateEvaluator
  * Policy: LoginPolicy, DeterministicTerminalState, SemanticTerminalState,
    CompositeTerminalEvaluator
  * Builder/CLI: resolve_decision_provider, build_login_agent, _parse_args, main
  * Flow adapter: SharedLoginFlow, LoginCapability
  * Constants: PROTOTYPE_GOAL, DEFAULT_GUIDANCE
"""

from __future__ import annotations

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
    DeterministicTerminalState,
    LoginPolicy,
    SemanticTerminalState,
)
from .builder import (
    build_login_agent,
    main,
    resolve_decision_provider,
    _parse_args,
)
from .flow import LoginCapability, SharedLoginFlow, _SignInTracer

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
    "build_login_agent",
    "main",
    "resolve_decision_provider",
]
