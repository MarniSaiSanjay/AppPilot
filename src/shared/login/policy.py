"""Runtime-customisable login policy for the shared login node.

A LoginPolicy is how a use case tells the shared login node WHAT it wants and
WHY, without the node ever learning any use-case-specific meaning. The shared
node owns HOW: it drives sign-in/onboarding and, before offering any action,
asks the policy's ordered terminal states whether login should stop.

Nothing here knows about Deeplink, FRI, or any specific use case - a use case
supplies its intent as natural-language terminal descriptions (or deterministic
predicates when it already has reliable structured evidence).

Precedence (deterministic):
  * Terminal states are evaluated BEFORE any action is offered, so reaching a
    terminal ALWAYS wins over dismissing/continuing an incidental screen.
  * Terminals are ordered by declaration: the first that reports "reached" wins.
"""

from __future__ import annotations

from typing import Callable

from .goal import PROTOTYPE_GOAL, DEFAULT_GUIDANCE


class DeterministicTerminalState:
    """Terminal reached when a caller-supplied predicate matches the screen.

    Pure and deterministic - no model call. Use when the use case already has
    reliable structured evidence (resource-ids, labels) for its terminal screen.
    Has no opinion on actionability (returns None) and is never dismissed: the
    composite checks it before the agent offers any action.
    """

    def __init__(self, predicate: "Callable[[object], bool]", *, name: str = "") -> None:
        self._predicate = predicate
        self.name = name

    def begin_run(self) -> None:  # symmetry with model-backed terminals
        pass

    def is_reached(self, goal: str, observation: object) -> bool:
        return self._predicate(observation) is True

    def has_actionable_step(self, observation: object):
        return None


class SemanticTerminalState:
    """Terminal reached when a natural-language screen description matches.

    This is the normal authoring surface for use cases: describe the terminal
    screen in plain language (e.g. "the First-Run / FRI screen is displayed").
    Model-backed via an injected evaluator exposing ``matches(description, obs)``
    (see SemanticStateEvaluator). When no evaluator is configured it is NEVER
    terminal (returns False), so an unconfigured model can never fabricate a
    terminal state. Has no opinion on actionability and is never dismissed.
    """

    def __init__(self, description: str, *, evaluator=None, name: str = "") -> None:
        self.description = description
        self._evaluator = evaluator
        self.name = name or description

    def with_evaluator(self, evaluator) -> "SemanticTerminalState":
        """Return a copy bound to ``evaluator`` (used by the builder to inject the
        shared semantic judge without the use case wiring a model itself)."""
        if self._evaluator is not None or evaluator is None:
            return self
        return SemanticTerminalState(
            self.description, evaluator=evaluator, name=self.name
        )

    def begin_run(self) -> None:
        begin = getattr(self._evaluator, "begin_run", None)
        if callable(begin):
            begin()

    def is_reached(self, goal: str, observation: object) -> bool:
        if self._evaluator is None:
            return False
        return bool(self._evaluator.matches(self.description, observation))

    def has_actionable_step(self, observation: object):
        return None


class CompositeTerminalEvaluator:
    """Ordered set of terminal states presented to the agent as ONE evaluator.

    Implements the agent's goal-evaluator interface. ``is_reached`` returns True
    as soon as any terminal (in declaration order) reports reached - so terminal
    detection happens before action selection and higher-priority terminals win.
    ``has_actionable_step`` returns the first terminal that expresses an opinion
    (non-None); if none do, it fails open (True) so the agent keeps driving, just
    as an unconstrained login does today.
    """

    def __init__(self, terminals) -> None:
        self._terminals = tuple(terminals)

    @property
    def terminals(self) -> tuple:
        return self._terminals

    def begin_run(self) -> None:
        for terminal in self._terminals:
            begin = getattr(terminal, "begin_run", None)
            if callable(begin):
                begin()

    def is_reached(self, goal: str, observation: object) -> bool:
        for terminal in self._terminals:
            if terminal.is_reached(goal, observation):
                return True
        return False

    def has_actionable_step(self, observation: object) -> bool:
        for terminal in self._terminals:
            verdict = terminal.has_actionable_step(observation)
            if verdict is not None:
                return bool(verdict)
        return True


class LoginPolicy:
    """A use case's runtime intent for the shared login node.

    Fields:
      * ``goal`` - natural-language objective handed to the agent at run time.
      * ``guidance`` - optional natural-language guidance for the agent.
      * ``terminals`` - ordered, use-case-specific terminal states (highest
        priority first). May be empty for the default login-completion behavior.
      * ``stop_at_login_completion`` - when True (default) the shared node's
        built-in login-completion terminal is appended as the lowest-priority
        fallback, so login still stops normally when the custom terminals never
        appear. This is the generic default; a use case may opt out.

    ``LoginPolicy.default()`` reproduces today's behavior exactly: the prototype
    login goal/guidance and only the built-in login-completion terminal.
    """

    def __init__(
        self,
        goal: str = PROTOTYPE_GOAL,
        guidance: "str | None" = DEFAULT_GUIDANCE,
        terminals=(),
        *,
        stop_at_login_completion: bool = True,
    ) -> None:
        self.goal = goal
        self.guidance = guidance
        self.terminals = tuple(terminals)
        self.stop_at_login_completion = stop_at_login_completion

    @classmethod
    def default(cls) -> "LoginPolicy":
        return cls(
            goal=PROTOTYPE_GOAL,
            guidance=DEFAULT_GUIDANCE,
            terminals=(),
            stop_at_login_completion=True,
        )
