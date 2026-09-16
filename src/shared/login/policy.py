"""Runtime-customisable login policy for the shared login node."""

from __future__ import annotations

from typing import Callable

from .goal import PROTOTYPE_GOAL, DEFAULT_GUIDANCE


class DeterministicTerminalState:
    """Terminal reached when a caller-supplied predicate matches."""

    def __init__(self, predicate: "Callable[[object], bool]", *, name: str = "") -> None:
        self._predicate = predicate
        self.name = name

    def begin_run(self) -> None:
        pass

    def is_reached(self, goal: str, observation: object) -> bool:
        return self._predicate(observation) is True

    def has_actionable_step(self, observation: object):
        return None


class SemanticTerminalState:
    """Terminal reached when a natural-language screen description matches."""

    def __init__(self, description: str, *, evaluator=None, name: str = "") -> None:
        self.description = description
        self._evaluator = evaluator
        self.name = name or description

    def with_evaluator(self, evaluator) -> "SemanticTerminalState":
        """Return a copy bound to ``evaluator``."""
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
    """Ordered terminal-state evaluator."""

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

    def finish_run(self, succeeded: bool) -> None:
        for terminal in self._terminals:
            finish = getattr(terminal, "finish_run", None)
            if callable(finish):
                finish(succeeded)

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

    def failure_reason(self, observation: object) -> "str | None":
        for terminal in self._terminals:
            get_reason = getattr(terminal, "failure_reason", None)
            if callable(get_reason):
                reason = get_reason(observation)
                if reason:
                    return str(reason)
        return None


class LoginPolicy:
    """A use case's runtime intent for the shared login node."""

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
