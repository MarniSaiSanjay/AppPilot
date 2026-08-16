"""Shared login flow adapter (use-case-agnostic).

Wraps the generic login AppPilotAgent as a small ``LoginCapability`` the caller
can invoke to prepare sign-in/onboarding, plus an observability tracer that
emits ``[LOGIN]`` execution-trace lines derived only from the agent's real
verdicts. No login UI is hardcoded and nothing here knows about Deeplink, FRI,
or any specific use case: the goal/guidance default to the shared prototype and
can be overridden by the caller.
"""

from __future__ import annotations

from typing import Protocol

try:  # package-relative (python -m src.shared.login.flow) vs top-level
    from ...apppilot.models import UIObservation
    from ...apppilot import logtags
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.models import UIObservation
    from apppilot import logtags

from .goal import DEFAULT_GUIDANCE, PROTOTYPE_GOAL


class LoginCapability(Protocol):
    def ensure_ready(self) -> bool:
        """Return True iff login preparation reached success. This is the sole
        signal the caller inspects; it never implies use-case success."""
        ...


class SharedLoginFlow:
    """Ensures the app is signed-in/ready via the SHARED generic login flow
    (AppPilotAgent + Brain). No login UI is hardcoded: the agent's goal evaluator
    reports success immediately when already signed in, otherwise the model
    drives each onboarding action. The SAME instance is reused across scenarios
    (DRY). The goal/guidance default to the shared prototype; a use case may pass
    its own so login stops at the boundary that use case cares about."""

    def __init__(
        self,
        agent,
        *,
        goal: str = PROTOTYPE_GOAL,
        guidance: "str | None" = DEFAULT_GUIDANCE,
    ) -> None:
        self._agent = agent
        self._goal = goal
        self._guidance = guidance
        # Observability only: wrap the agent's goal evaluator so the [LOGIN] trace
        # reflects real verdicts. The wrapper returns each verdict verbatim, so
        # behavior is unchanged. Guard against double-wrapping if reused.
        self._tracer: _SignInTracer | None = None
        evaluator = getattr(agent, "_goal_evaluator", None)
        if evaluator is not None and not isinstance(evaluator, _SignInTracer):
            self._tracer = _SignInTracer(evaluator)
            agent._goal_evaluator = self._tracer

    def ensure_ready(self) -> bool:
        # Reset the per-run trace state so each login attempt reports its own
        # already/required/completed verdict (the login flow is reused across the
        # installed batch and every uninstalled attempt).
        if self._tracer is not None:
            self._tracer.begin_run()
        # Propagate the agent's verdict verbatim (True = login goal reached,
        # False = preparation failed) - do NOT swallow it.
        return bool(self._agent.run(self._goal, self._guidance))


class _SignInTracer:
    """Observability wrapper around the shared login goal evaluator.

    Emits [LOGIN] trace lines derived only from the real verdicts the underlying
    evaluator returns, passing those verdicts through unchanged:

    * the first verdict decides "Already signed in" vs "Sign-in required";
    * a later transition to signed-in is the actual completion.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._seen_first = False
        self._required = False

    def begin_run(self) -> None:
        self._seen_first = False
        self._required = False
        # Reset any per-run state the wrapped evaluator exposes. The boundary
        # evaluator is stateless (no-op here); kept as a forward-safe hook.
        inner_begin = getattr(self._inner, "begin_run", None)
        if callable(inner_begin):
            inner_begin()

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        reached = self._inner.is_reached(goal, observation)
        if not self._seen_first:
            self._seen_first = True
            if reached:
                logtags.trace(
                    "Already signed in - no login actions required", logtags.LOGIN
                )
            else:
                self._required = True
                logtags.trace(
                    "Sign-in required - starting shared login flow", logtags.LOGIN
                )
        elif reached and self._required:
            self._required = False
            logtags.trace("Authentication/onboarding boundary reached", logtags.LOGIN)
            logtags.trace("Login goal reached: true", logtags.LOGIN)
            logtags.trace("Returning control to use case", logtags.LOGIN)
        return reached
