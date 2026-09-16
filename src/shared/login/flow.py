"""Shared login flow adapter."""

from __future__ import annotations
from collections.abc import Callable
from typing import Protocol

try:
    from ...apppilot.models import UIObservation
    from ...apppilot import logtags
except ImportError:
    from apppilot.models import UIObservation
    from apppilot import logtags

from .goal import DEFAULT_GUIDANCE, PROTOTYPE_GOAL


class LoginCapability(Protocol):
    def ensure_ready(self) -> bool:
        """Return True iff login preparation reached success."""
        ...


class SharedLoginFlow:
    """Ensures the app is signed in and ready."""

    def __init__(
        self,
        agent,
        *,
        goal: str = PROTOTYPE_GOAL,
        guidance: "str | None" = DEFAULT_GUIDANCE,
        loading_recovery: Callable[[], None] | None = None,
    ) -> None:
        self._agent = agent
        self._goal = goal
        self._guidance = guidance
        self._loading_recovery = loading_recovery
        self._restarted_last_run = False
        self._tracer: _SignInTracer | None = None
        evaluator = getattr(agent, "_goal_evaluator", None)
        if evaluator is not None and not isinstance(evaluator, _SignInTracer):
            self._tracer = _SignInTracer(evaluator)
            agent._goal_evaluator = self._tracer

    def ensure_ready(self) -> bool:
        return self._ensure_ready(allow_recovery=True)

    def ensure_ready_without_relaunch(self) -> bool:
        """Prepare login without recovery that relaunches the target app."""
        return self._ensure_ready(allow_recovery=False)

    def _ensure_ready(self, *, allow_recovery: bool) -> bool:
        self._restarted_last_run = False
        max_runs = (
            2
            if allow_recovery and self._loading_recovery is not None
            else 1
        )
        for run in range(max_runs):
            if self._tracer is not None:
                self._tracer.begin_run()
            if self._agent.run(self._goal, self._guidance):
                return True

            reason = getattr(self._agent, "last_failure_reason", None)
            loading_exhaustion = self._is_loading_exhaustion(reason)
            action_stall = self._is_action_stall(reason)
            if (
                run > 0
                or not allow_recovery
                or self._loading_recovery is None
                or not (loading_exhaustion or action_stall)
            ):
                return False
            self._restarted_last_run = True
            self._loading_recovery()

    @property
    def restarted_last_run(self) -> bool:
        """Whether the most recent login call relaunched the app."""
        return self._restarted_last_run

    @property
    def last_failure_reason(self) -> "str | None":
        """The agent's precise reason for the most recent failed login run."""
        reason = getattr(self._agent, "last_failure_reason", None)
        return str(reason) if reason else None

    @staticmethod
    def _is_loading_exhaustion(reason: object) -> bool:
        text = str(reason or "")
        return (
            text.startswith("no actionable step appeared after ")
            and "loading/transition state" in text
        )

    @staticmethod
    def _is_action_stall(reason: object) -> bool:
        return str(reason or "").startswith(
            "agent appears stuck: no meaningful UI change for "
        )


class _SignInTracer:
    """Emits login trace lines from goal-evaluator verdicts."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._seen_first = False
        self._required = False

    def begin_run(self) -> None:
        self._seen_first = False
        self._required = False
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

    def has_actionable_step(self, observation: UIObservation) -> bool:
        return bool(self._inner.has_actionable_step(observation))

    def failure_reason(self, observation: UIObservation) -> "str | None":
        get_reason = getattr(self._inner, "failure_reason", None)
        if not callable(get_reason):
            return None
        reason = get_reason(observation)
        return str(reason) if reason else None

    def finish_run(self, succeeded: bool) -> None:
        finish = getattr(self._inner, "finish_run", None)
        if callable(finish):
            finish(succeeded)
