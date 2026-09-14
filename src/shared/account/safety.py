"""Narrow safety policy for deterministic account-session navigation."""

from __future__ import annotations

import re
from enum import Enum

try:  # package-relative vs top-level (src on sys.path)
    from ...apppilot.models import Action, ActionKind, UIElement, UIObservation
except ImportError:
    from apppilot.models import Action, ActionKind, UIElement, UIObservation


def _element_text(element: UIElement) -> str:
    return " ".join(
        value
        for value in (
            element.text,
            element.accessibility_text,
            element.hint_text,
            element.label,
            element.resource_id,
        )
        if value
    ).casefold()


def _matches_identifier(
    element: UIElement, identifier: str | None
) -> bool:
    target = (identifier or "").strip().casefold()
    if not target:
        return False
    boundary = r"A-Za-z0-9._%+\-"
    return (
        re.search(
            rf"(?<![{boundary}]){re.escape(target)}(?![{boundary}])",
            _element_text(element),
        )
        is not None
    )


class AccountActionPurpose(str, Enum):
    MENU = "menu"
    DRAWER_ACCOUNT = "drawer_account"
    SETTINGS_ACCOUNT = "settings_account"
    EXISTING_ACCOUNT = "existing_account"
    ADD_ACCOUNT = "add_account"
    OVERLAY_PERMISSION = "overlay_permission"
    OVERLAY_BACK = "overlay_back"


class AccountSafetyValidator:
    """Allow only the controls required to add or switch an app account."""

    _PROHIBITED = (
        "sign out",
        "log out",
        "remove account",
        "delete account",
        "manage account",
    )

    def validate(
        self,
        purpose: AccountActionPurpose,
        action: Action,
        observation: UIObservation,
        *,
        target_identifier: str | None = None,
    ) -> None:
        if purpose == AccountActionPurpose.OVERLAY_BACK:
            if action.kind != ActionKind.PRESS_BACK or action.target_id is not None:
                raise ValueError("overlay return requires a back action")
            return
        if action.kind != ActionKind.TAP:
            raise ValueError("account navigation permits tap actions only")

        target = observation.find(action.target_id)
        if target is None or not target.enabled or not target.clickable:
            raise ValueError("account navigation target is unavailable")
        if self.is_prohibited(target):
            raise ValueError("account navigation target is prohibited")

        if purpose == AccountActionPurpose.MENU:
            allowed = self._is_menu(target)
        elif purpose == AccountActionPurpose.DRAWER_ACCOUNT:
            allowed = self._is_drawer_account(target, observation)
        elif purpose == AccountActionPurpose.SETTINGS_ACCOUNT:
            allowed = self._is_settings_account(target)
        elif purpose == AccountActionPurpose.EXISTING_ACCOUNT:
            allowed = _matches_identifier(target, target_identifier) or any(
                _matches_identifier(item, target_identifier)
                and self._is_descendant(item, target, observation)
                for item in observation.elements
            )
        elif purpose == AccountActionPurpose.ADD_ACCOUNT:
            allowed = self._is_add_account(target) or any(
                self._is_add_account(item)
                and self._is_descendant(item, target, observation)
                for item in observation.elements
            )
        elif purpose == AccountActionPurpose.OVERLAY_PERMISSION:
            allowed = self._is_overlay_enable(target, observation)
        else:
            allowed = False
        if not allowed:
            raise ValueError("account navigation target is not allowed")

    @classmethod
    def is_prohibited(cls, element: UIElement) -> bool:
        tokens = re.sub(r"[^a-z0-9]+", " ", _element_text(element)).split()
        for term in cls._PROHIBITED:
            parts = term.split()
            size = len(parts)
            if any(
                tokens[index:index + size] == parts
                for index in range(len(tokens) - size + 1)
            ):
                return True
            if term.replace(" ", "") in tokens:
                return True
        return False

    @classmethod
    def _is_menu(cls, element: UIElement) -> bool:
        allowed = ("menu", "navigation menu", "open navigation", "more")
        return any(
            value.strip().casefold() in allowed
            for value in (
                element.text,
                element.accessibility_text,
                element.hint_text,
                element.label,
            )
            if value
        )

    @classmethod
    def _is_drawer_account(
        cls, element: UIElement, observation: UIObservation
    ) -> bool:
        if any(
            value.strip().casefold() in ("account", "profile", "settings")
            for value in (
                element.text,
                element.accessibility_text,
                element.hint_text,
                element.label,
            )
            if value
        ):
            return True
        bounded = [
            item
            for item in observation.elements
            if item.enabled
            and item.clickable
            and item.bounds is not None
            and not cls.is_prohibited(item)
        ]
        if bounded and element.bounds is not None:
            return element.bounds[3] == max(item.bounds[3] for item in bounded)
        return False

    @classmethod
    def _is_settings_account(cls, element: UIElement) -> bool:
        text = _element_text(element)
        return "@" in text or "account" in text or "profile" in text

    @classmethod
    def _is_add_account(cls, element: UIElement) -> bool:
        text = _element_text(element)
        return any(
            phrase in text
            for phrase in (
                "add an account",
                "add account",
                "sign in with another account",
                "use another account",
            )
        )

    @classmethod
    def _is_overlay_enable(
        cls, element: UIElement, observation: UIObservation
    ) -> bool:
        text = _element_text(element)
        if (
            "allow display over other apps" not in text
            and "switch" not in element.class_name.casefold()
        ):
            return False
        states = [
            item.checked
            for item in observation.elements
            if item.checked is not None
            and (
                item.element_id == element.element_id
                or cls._is_descendant(item, element, observation)
            )
        ]
        return states == [False]

    @staticmethod
    def _is_descendant(
        candidate: UIElement,
        ancestor: UIElement,
        observation: UIObservation,
    ) -> bool:
        current = candidate
        while current.parent_id:
            if current.parent_id == ancestor.element_id:
                return True
            parent = observation.find(current.parent_id)
            if parent is None:
                return False
            current = parent
        return False
