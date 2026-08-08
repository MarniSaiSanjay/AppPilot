"""Credential-safety inference and the SafetyValidator action gate."""

from __future__ import annotations

from .models import Action, ActionKind, CredentialKind, UIElement, UIObservation


def infer_credential_kind(
    resource_id: str = "",
    hint_text: str = "",
    class_name: str = "",
    extra: str = "",
) -> CredentialKind | None:
    """Infer whether an input field is a username/email or password field.

    Inference uses only stable, non-secret UI signals (resource id, hint,
    class name, and any caller-supplied extra identifiers) and never the field's
    live text value. Shared by the observer (to redact secrets) and the safety
    validator (to build/validate credential actions).
    """
    haystack = " ".join((resource_id, hint_text, class_name, extra)).casefold()
    password_markers = ("password", "passwd", "textpassword", "i0118")
    if any(marker in haystack for marker in password_markers):
        return CredentialKind.PASSWORD
    username_markers = (
        "email",
        "username",
        "user name",
        "phone",
        "loginfmt",
        "i0116",
        "emailtext",
        "upn",
    )
    if any(marker in haystack for marker in username_markers):
        return CredentialKind.USERNAME
    return None



class SafetyValidator:
    _PROHIBITED_TERMS = (
        "buy",
        "purchase",
        "pay",
        "subscribe",
        "checkout",
        "place order",
        "delete",
        "erase",
        "clear data",
        "remove account",
        "sign out",
        "log out",
        "factory reset",
        "uninstall",
        "security settings",
        "change password",
        "reset password",
        "manage account",
        "grant permission",
        "permission_allow",
        "allow access",
        "while using the app",
        "precise location",
        "camera",
        "microphone",
        "contacts",
    )

    def available_actions(self, observation: UIObservation) -> tuple[Action, ...]:
        actions: list[Action] = []
        for element in observation.elements:
            if (
                not element.enabled
                or self._is_prohibited(element)
                or not (element.resource_id or element.selector_text)
            ):
                continue
            if element.clickable:
                actions.append(Action(ActionKind.TAP, target_id=element.element_id))
            if element.is_input:
                actions.append(
                    Action(
                        ActionKind.INPUT_TEXT,
                        target_id=element.element_id,
                        credential_kind=self.credential_kind(element),
                    )
                )
        actions.append(Action(ActionKind.PRESS_BACK))
        return tuple(actions)

    def validate(self, action: Action, observation: UIObservation) -> None:
        if action.kind == ActionKind.PRESS_BACK:
            if action.target_id is not None:
                raise ValueError("Press-back actions cannot specify a UI target")
            return

        target = observation.find(action.target_id)
        if target is None:
            raise ValueError("Action target does not exist in the current observation")
        if not target.enabled:
            raise ValueError("Action target is disabled")
        if self._is_prohibited(target):
            raise ValueError("Action target is prohibited by the safety policy")
        if not (target.resource_id or target.selector_text):
            raise ValueError("Action target has no safe Maestro selector")

        if action.kind == ActionKind.TAP and not target.clickable:
            raise ValueError("Tap target is not clickable")
        if action.kind == ActionKind.INPUT_TEXT:
            if not target.is_input:
                raise ValueError("Text input target is not an observed input field")
            if action.credential_kind is not None:
                # The secret is resolved locally after validation, so input_text
                # is intentionally empty here; verify the field really is that
                # kind of credential field so the model cannot redirect it.
                if self.credential_kind(target) != action.credential_kind:
                    raise ValueError(
                        "Credential input kind does not match the observed field"
                    )
            elif not action.input_text:
                raise ValueError("Text input action requires non-empty text")

    @staticmethod
    def credential_kind(element: UIElement) -> CredentialKind | None:
        """Safely infer whether an input is a username/email or password field.

        Delegates to :func:`infer_credential_kind`, which uses only non-secret
        UI signals. Detection relies on resource id / hint / class, all of which
        survive the observer's redaction of credential-field values.
        """
        if not element.is_input:
            return None
        return infer_credential_kind(
            element.resource_id,
            element.hint_text,
            element.class_name,
            f"{element.accessibility_text} {element.label}",
        )

    def _is_prohibited(self, element: UIElement) -> bool:
        target = f"{element.label} {element.resource_id}".casefold()
        return any(term in target for term in self._PROHIBITED_TERMS)
