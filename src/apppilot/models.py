"""Shared domain data contracts for AppPilot (UI, actions, runtime state)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ActionKind(str, Enum):
    TAP = "tap"
    INPUT_TEXT = "input_text"
    PRESS_BACK = "press_back"


class CredentialKind(str, Enum):
    """A non-UI secret the agent can enter without the model knowing its value."""

    USERNAME = "username"
    PASSWORD = "password"


@dataclass(frozen=True)
class UIElement:
    element_id: str
    parent_id: str | None
    text: str
    accessibility_text: str
    hint_text: str
    resource_id: str
    class_name: str
    clickable: bool
    enabled: bool
    is_input: bool
    label: str
    bounds: "tuple[int, int, int, int] | None" = None

    @property
    def selector_text(self) -> str:
        return self.text or self.accessibility_text or self.hint_text or self.label

    @property
    def own_text(self) -> str:
        """Text belonging to THIS node (not merged from a descendant).

        Matching a clickable node by a label that actually lives on a child can
        tap the child (often zero/opaque bounds in Compose) instead. Only this
        node's own text is safe to drive a text selector."""
        return self.text or self.accessibility_text or self.hint_text

    @property
    def center(self) -> "tuple[int, int] | None":
        if self.bounds is None:
            return None
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)



@dataclass(frozen=True)
class UIObservation:
    elements: tuple[UIElement, ...]

    def find(self, element_id: str | None) -> UIElement | None:
        if element_id is None:
            return None
        return next(
            (element for element in self.elements if element.element_id == element_id),
            None,
        )

    def describe(self, limit: int = 20) -> str:
        relevant = [
            element
            for element in self.elements
            if element.label or element.resource_id or element.clickable or element.is_input
        ]
        if not relevant:
            return "<no relevant UI elements>"

        lines = []
        for element in relevant[:limit]:
            traits = []
            if element.clickable:
                traits.append("clickable")
            if element.is_input:
                traits.append("input")
            if not element.enabled:
                traits.append("disabled")
            details = f'label="{element.label}"' if element.label else "label=<none>"
            if element.resource_id:
                details += f' id="{element.resource_id}"'
            if element.parent_id:
                details += f" parent={element.parent_id}"
            suffix = f" [{', '.join(traits)}]" if traits else ""
            lines.append(f"- {element.element_id}: {details}{suffix}")

        hidden_count = len(relevant) - len(lines)
        if hidden_count:
            lines.append(f"- ... {hidden_count} additional relevant elements omitted")
        return "\n".join(lines)



@dataclass(frozen=True)
class Action:
    kind: ActionKind
    target_id: str | None = None
    input_text: str | None = None
    # For credential inputs the model only requests the *kind*; AppPilot resolves
    # the secret value locally, so ``input_text`` stays ``None`` for these.
    credential_kind: CredentialKind | None = None

    def describe(self, observation: UIObservation) -> str:
        if self.kind == ActionKind.PRESS_BACK:
            return "press back"
        target = observation.find(self.target_id)
        if self.kind == ActionKind.INPUT_TEXT:
            if self.credential_kind is not None:
                # Never include the field's live value; use only a stable, safe
                # descriptor (resource id or hint), or the element id.
                if target is not None:
                    descriptor = target.resource_id or target.hint_text or self.target_id
                else:
                    descriptor = self.target_id
                return f"input the {self.credential_kind.value} into {descriptor}"
            target_label = target.label if target else self.target_id
            return f'input text into {self.target_id} ("{target_label}")'
        target_label = target.label if target else self.target_id
        return f'tap {self.target_id} ("{target_label}")'



@dataclass(frozen=True)
class ExecutionContext:
    """State the agent shares with the model on every decision request."""

    step: int
    max_steps: int
    history: tuple[str, ...] = ()



class RuntimeContext:
    """Secure, non-UI runtime test data such as credentials.

    Never part of :class:`DecisionRequest`: never in prompts, observations, model
    responses, or logs. The agent resolves a credential locally and passes it
    straight to Maestro. ``repr`` deliberately hides values.
    """

    def __init__(self, credentials: dict[CredentialKind, str]) -> None:
        self._credentials = {kind: value for kind, value in credentials.items() if value}

    @classmethod
    def from_env(cls, env: dict | None = None) -> "RuntimeContext":
        env = os.environ if env is None else env
        credentials: dict[CredentialKind, str] = {}
        username = env.get("APPPILOT_USERNAME")
        password = env.get("APPPILOT_PASSWORD")
        if username:
            credentials[CredentialKind.USERNAME] = username
        if password:
            credentials[CredentialKind.PASSWORD] = password
        return cls(credentials)

    def has(self, kind: CredentialKind) -> bool:
        return kind in self._credentials

    def resolve(self, kind: CredentialKind) -> str:
        """Return the secret value for ``kind`` or raise ``KeyError`` (no value)."""
        return self._credentials[kind]

    def __repr__(self) -> str:
        available = sorted(kind.value for kind in self._credentials)
        return f"RuntimeContext(available={available})"



class GoalEvaluator(Protocol):
    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        ...
