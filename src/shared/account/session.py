"""Deterministic, secret-safe Android account-session preparation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

try:  # package-relative vs top-level (src on sys.path)
    from ...apppilot.models import (
        Action,
        ActionKind,
        CredentialKind,
        UIElement,
        UIObservation,
    )
except ImportError:
    from apppilot.models import (
        Action,
        ActionKind,
        CredentialKind,
        UIElement,
        UIObservation,
    )

from . import AccountPreparationKind, AccountPreparationResult
from .safety import (
    AccountActionPurpose,
    AccountSafetyValidator,
    _element_text,
    _matches_identifier,
)
from ..credentials import CredentialProfile
from ..login.flow import LoginCapability


class _Observer(Protocol):
    def observe(self) -> UIObservation:
        ...


class _Executor(Protocol):
    def execute(
        self,
        action: Action,
        observation: UIObservation,
        secret: str | None = None,
    ) -> None:
        ...


class _NavigationError(RuntimeError):
    pass


class AndroidAccountSession:
    """Add, switch, and verify app accounts without exposing identifiers."""

    def __init__(
        self,
        observer: _Observer,
        executor: _Executor,
        login_flow_for_profile: Callable[[CredentialProfile], LoginCapability],
        *,
        settle_seconds: float = 1.0,
        account_load_seconds: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
        safety_validator: AccountSafetyValidator | None = None,
    ) -> None:
        self._observer = observer
        self._executor = executor
        self._login_flow_for_profile = login_flow_for_profile
        self._settle_seconds = settle_seconds
        self._account_load_seconds = account_load_seconds
        self._sleep = sleep
        self._safety = safety_validator or AccountSafetyValidator()

    def ensure_active(
        self, profile: CredentialProfile
    ) -> AccountPreparationResult:
        try:
            target = profile.runtime_context.resolve(CredentialKind.USERNAME)
        except KeyError:
            return AccountPreparationResult.failed(
                "credential profile has no username"
            )

        try:
            settings = self._open_settings()
            if self._contains(settings, target):
                return AccountPreparationResult.ready(
                    AccountPreparationKind.ALREADY_ACTIVE
                )

            sheet = self._open_account_sheet(settings)
            existing = self._find_matching_control(sheet, target)
            if existing is not None:
                self._tap(
                    sheet,
                    existing,
                    AccountActionPurpose.EXISTING_ACCOUNT,
                    target_identifier=target,
                )
                self._sleep(self._account_load_seconds)
                kind = AccountPreparationKind.SWITCHED
            else:
                add_account = self._find_text_control(
                    sheet, ("add an account", "add account")
                )
                if add_account is None:
                    raise _NavigationError("account sheet has no add action")
                self._tap(
                    sheet,
                    add_account,
                    AccountActionPurpose.ADD_ACCOUNT,
                )
                self._sleep(self._settle_seconds)
                if not self._login_flow_for_profile(profile).ensure_ready():
                    return AccountPreparationResult.failed(
                        "account authentication did not complete"
                    )
                self._sleep(self._account_load_seconds)
                kind = AccountPreparationKind.ADDED

            verified = self._open_settings()
            if not self._contains(verified, target):
                return AccountPreparationResult.failed(
                    "active account verification failed"
                )
            return AccountPreparationResult.ready(kind)
        except _NavigationError as error:
            return AccountPreparationResult.failed(str(error))
        except RuntimeError:
            return AccountPreparationResult.failed(
                "account navigation failed"
            )

    def _open_settings(self) -> UIObservation:
        home = self._observer.observe()
        menu = self._find_text_control(
            home, ("menu", "navigation menu", "open navigation")
        )
        if menu is None:
            raise _NavigationError("home menu is unavailable")
        self._tap(home, menu, AccountActionPurpose.MENU)
        self._sleep(self._settle_seconds)

        drawer = self._observer.observe()
        account_row = self._find_drawer_account_row(drawer)
        if account_row is None:
            raise _NavigationError("drawer account entry is unavailable")
        self._tap(
            drawer,
            account_row,
            AccountActionPurpose.DRAWER_ACCOUNT,
        )
        self._sleep(self._settle_seconds)
        return self._observer.observe()

    def _open_account_sheet(self, settings: UIObservation) -> UIObservation:
        account_card = self._find_settings_account_card(settings)
        if account_card is None:
            raise _NavigationError("settings account entry is unavailable")
        self._tap(
            settings,
            account_card,
            AccountActionPurpose.SETTINGS_ACCOUNT,
        )
        self._sleep(self._settle_seconds)

        observation = self._observer.observe()
        if self._is_overlay_settings(observation):
            overlay_control = self._find_overlay_switch(observation)
            if overlay_control is None:
                raise _NavigationError(
                    "overlay permission state is unavailable"
                )
            switch, enabled = overlay_control
            if not enabled:
                self._tap(
                    observation,
                    switch,
                    AccountActionPurpose.OVERLAY_PERMISSION,
                )
                self._sleep(self._settle_seconds)
            back = Action(ActionKind.PRESS_BACK)
            self._execute(
                observation,
                back,
                AccountActionPurpose.OVERLAY_BACK,
            )
            self._sleep(self._settle_seconds)
            observation = self._observer.observe()
        return observation

    def _tap(
        self,
        observation: UIObservation,
        element: UIElement,
        purpose: AccountActionPurpose,
        *,
        target_identifier: str | None = None,
    ) -> None:
        action = Action(ActionKind.TAP, target_id=element.element_id)
        self._execute(
            observation,
            action,
            purpose,
            target_identifier=target_identifier,
        )

    def _execute(
        self,
        observation: UIObservation,
        action: Action,
        purpose: AccountActionPurpose,
        *,
        target_identifier: str | None = None,
    ) -> None:
        try:
            self._safety.validate(
                purpose,
                action,
                observation,
                target_identifier=target_identifier,
            )
        except ValueError as error:
            raise _NavigationError(str(error)) from error
        self._executor.execute(action, observation)

    @staticmethod
    def _contains(observation: UIObservation, value: str) -> bool:
        return any(
            _matches_identifier(element, value)
            for element in observation.elements
        )

    @classmethod
    def _find_text_control(
        cls,
        observation: UIObservation,
        labels: tuple[str, ...],
    ) -> UIElement | None:
        normalized = tuple(label.casefold() for label in labels)
        for element in observation.elements:
            text = _element_text(element)
            if element.enabled and element.clickable and any(
                label == text or label in text for label in normalized
            ):
                return element
        for element in observation.elements:
            text = _element_text(element)
            if any(label == text or label in text for label in normalized):
                control = cls._clickable_ancestor(observation, element)
                if control is not None:
                    return control
        return None

    @classmethod
    def _find_matching_control(
        cls,
        observation: UIObservation,
        value: str,
    ) -> UIElement | None:
        for element in observation.elements:
            if _matches_identifier(element, value):
                if element.enabled and element.clickable:
                    return element
                control = cls._clickable_ancestor(observation, element)
                if control is not None:
                    return control
        return None

    @staticmethod
    def _clickable_ancestor(
        observation: UIObservation,
        element: UIElement,
    ) -> UIElement | None:
        current = element
        while current.parent_id:
            parent = observation.find(current.parent_id)
            if parent is None:
                return None
            if parent.enabled and parent.clickable:
                return parent
            current = parent
        return None

    def _find_drawer_account_row(
        self, observation: UIObservation
    ) -> UIElement | None:
        candidates = [
            element
            for element in observation.elements
            if element.enabled
            and element.clickable
            and element.bounds is not None
            and not self._safety.is_prohibited(element)
        ]
        if not candidates:
            return self._find_text_control(
                observation, ("account", "profile")
            )
        return max(
            candidates,
            key=lambda element: (
                element.bounds[3],
                "account" in _element_text(element)
                or "profile" in _element_text(element),
            ),
        )

    @classmethod
    def _find_settings_account_card(
        cls, observation: UIObservation
    ) -> UIElement | None:
        email_controls = [
            element
            for element in observation.elements
            if element.enabled
            and element.clickable
            and "@" in _element_text(element)
        ]
        if email_controls:
            return min(
                email_controls,
                key=lambda element: (
                    element.bounds[1] if element.bounds else 10**9
                ),
            )
        return cls._find_text_control(observation, ("account", "profile"))

    @classmethod
    def _is_overlay_settings(cls, observation: UIObservation) -> bool:
        return any(
            phrase in _element_text(element)
            for element in observation.elements
            for phrase in (
                "display over other apps",
                "allow display over other apps",
            )
        )

    @classmethod
    def _find_overlay_switch(
        cls, observation: UIObservation
    ) -> "tuple[UIElement, bool] | None":
        for element in observation.elements:
            if (
                "switch" in element.class_name.casefold()
                and element.checked is not None
            ):
                control = (
                    element
                    if element.enabled and element.clickable
                    else cls._clickable_ancestor(observation, element)
                )
                if control is not None:
                    return control, element.checked
        for element in observation.elements:
            text = _element_text(element)
            if (
                element.enabled
                and element.clickable
                and "allow display over other apps" in text
                and element.checked is not None
            ):
                return element, element.checked
        return None
