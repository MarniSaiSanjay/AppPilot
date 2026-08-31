"""Login-specific known decisions over the generic adaptive replay core."""

from __future__ import annotations

import re
from pathlib import Path

try:  # package-relative vs top-level (src on sys.path)
    from ...apppilot.adaptive_decision_cache import AdaptiveDecisionCache
    from ...apppilot import logtags
    from ...apppilot.brain import (
        DecisionRequest,
        ModelDecisionProvider,
    )
    from ...apppilot.models import Action, ActionKind, CredentialKind, UIElement
    from ...apppilot.safety import infer_credential_kind
except ImportError:
    from apppilot.adaptive_decision_cache import AdaptiveDecisionCache
    from apppilot import logtags
    from apppilot.brain import DecisionRequest, ModelDecisionProvider
    from apppilot.models import Action, ActionKind, CredentialKind, UIElement
    from apppilot.safety import infer_credential_kind


_EMAIL = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")


class LoginDecisionCache(AdaptiveDecisionCache):
    """Add login controls and credential sequencing to generic replay.

    Seeded decisions cover only unique, unambiguous controls. Model decisions
    are learned and replayed by ``AdaptiveDecisionCache`` only after the
    complete login run succeeds.
    """

    def __init__(
        self,
        fallback: ModelDecisionProvider,
        *,
        cache_path: Path | None = None,
    ) -> None:
        super().__init__(
            fallback,
            cache_namespace="login",
            cache_path=cache_path,
            log_tag=logtags.LOGIN,
            known_reason="Matched a known safe login control.",
            cached_reason=(
                "Reused a decision from a previous successful login."
            ),
        )
        self._last_credential: CredentialKind | None = None

    def begin_run(self) -> None:
        super().begin_run()
        self._last_credential = None

    def finish_run(self, succeeded: bool) -> None:
        try:
            super().finish_run(succeeded)
        finally:
            self._last_credential = None

    def record_executed(self, action: Action) -> None:
        self._last_credential = action.credential_kind

    def _seeded_decision(self, request: DecisionRequest) -> Action | None:
        actions = request.available_actions

        for kind in (CredentialKind.USERNAME, CredentialKind.PASSWORD):
            match = self._unique(
                action
                for action in actions
                if action.kind == ActionKind.INPUT_TEXT
                and action.credential_kind == kind
            )
            if match is not None:
                return match

        for phrases in (
            ("use your password", "use password instead"),
            ("continue with microsoft",),
            ("use another account",),
        ):
            match = self._unique_matching_tap(request, phrases)
            if match is not None:
                return match

        if self._has_passwordless_prompt(request):
            match = self._unique_matching_tap(
                request,
                ("other ways to sign in", "sign in another way"),
            )
            if match is not None:
                return match

        if self._credential_still_present(request):
            match = self._unique_submit(request)
            if match is not None:
                return match

        for screen_markers, control_phrases in (
            (("microsoft respects your privacy",), ("next",)),
            (("your privacy option",), ("close", "ok", "got it")),
            (
                ("getting better together",),
                ("don't send optional data", "don’t send optional data"),
            ),
            (("powering your experiences",), ("next",)),
            (("don't miss anything", "don’t miss anything"), ("not now",)),
            (("let's get started", "lets get started"), ("close",)),
        ):
            if self._screen_contains(request, screen_markers):
                match = self._unique_matching_tap(request, control_phrases)
                if match is not None:
                    return match
        return None

    def _credential_still_present(self, request: DecisionRequest) -> bool:
        if self._last_credential is None:
            return False
        return any(
            element.is_input
            and infer_credential_kind(
                element.resource_id,
                element.hint_text,
                element.class_name,
                f"{element.accessibility_text} {element.label}",
            )
            == self._last_credential
            for element in request.observation.elements
        )

    @classmethod
    def _unique_matching_tap(
        cls,
        request: DecisionRequest,
        phrases: tuple[str, ...],
    ) -> Action | None:
        return cls._unique(
            action
            for action in request.available_actions
            if action.kind == ActionKind.TAP
            and any(
                phrase in cls._target_text(request, action)
                for phrase in phrases
            )
        )

    @classmethod
    def _unique_submit(cls, request: DecisionRequest) -> Action | None:
        def is_submit(action: Action) -> bool:
            if action.kind != ActionKind.TAP:
                return False
            target = request.observation.find(action.target_id)
            if target is None:
                return False
            label = (target.own_text or target.label).strip().casefold()
            resource_id = target.resource_id.strip().casefold()
            return label in ("next", "sign in") or resource_id in (
                "nextbutton",
                "idsibutton9",
            )

        return cls._unique(
            action for action in request.available_actions if is_submit(action)
        )

    @staticmethod
    def _unique(actions) -> Action | None:
        matches = tuple(actions)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _target_text(request: DecisionRequest, action: Action) -> str:
        target = request.observation.find(action.target_id)
        if target is None:
            return ""
        return " ".join(
            (
                target.resource_id,
                target.text,
                target.accessibility_text,
                target.hint_text,
                target.label,
            )
        ).casefold()

    @staticmethod
    def _has_passwordless_prompt(request: DecisionRequest) -> bool:
        markers = ("passkey", "verification code", "send a code", "authenticator")
        return any(
            marker in LoginDecisionCache._element_text(element)
            for element in request.observation.elements
            for marker in markers
        )

    @staticmethod
    def _screen_contains(
        request: DecisionRequest,
        markers: tuple[str, ...],
    ) -> bool:
        return any(
            marker in LoginDecisionCache._element_text(element)
            for element in request.observation.elements
            for marker in markers
        )

    @staticmethod
    def _element_text(element: UIElement) -> str:
        return _EMAIL.sub(
            "<account>",
            AdaptiveDecisionCache._element_text(element),
        )
