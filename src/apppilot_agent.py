from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence


APP_ID = "com.microsoft.office.officehubrow"
REPO_ROOT = Path(__file__).resolve().parent.parent
# Safety bound on the number of actions per run, to prevent an infinite agent
# loop. Configurable via --max-actions or the APPPILOT_MAX_ACTIONS env var; this
# is only the fallback default.
DEFAULT_MAX_ACTIONS = 30
# Stop with a controlled FAIL after this many actions without a meaningful UI
# change. Configurable via --max-stuck-actions or APPPILOT_MAX_STUCK_ACTIONS.
DEFAULT_MAX_STUCK_ACTIONS = 5
# The agent reasons over the observed UI and drives Maestro action-by-action;
# it does not execute any prewritten Maestro flow.
PROTOTYPE_GOAL = (
    "Complete authentication and onboarding and reach a usable signed-in "
    "Microsoft 365 Copilot experience, without executing an unintended "
    "suggested prompt."
)
DEFAULT_GUIDANCE = (
    "Advance through any sign-in and onboarding screens toward a usable "
    "signed-in Microsoft 365 Copilot experience. When a sign-in email/username "
    "or password field is shown, choose the matching credential input action "
    "and set input_kind accordingly; AppPilot supplies the actual value "
    "securely, so never type a credential yourself. Dismiss incidental "
    "interruptions (for example a 'Save password' prompt) with the safest "
    "non-destructive option and keep going. If an introductory screen shows a "
    "random suggested prompt with a Send button and a close (X) control, close "
    "it instead of sending the suggestion. A prompt already present in the "
    "composer is fine to leave as-is; do not press Send unless the goal "
    "explicitly requires submitting it."
)


class ActionKind(str, Enum):
    TAP = "tap"
    INPUT_TEXT = "input_text"
    PRESS_BACK = "press_back"


class CredentialKind(str, Enum):
    """A non-UI secret the agent can enter without the model knowing its value."""

    USERNAME = "username"
    PASSWORD = "password"


# Env var used to hand a resolved secret to the Maestro subprocess. The
# MAESTRO_ prefix lets Maestro read it from the process environment via
# ${...} interpolation, so the value never appears in the flow YAML, in argv,
# or in logs.
MAESTRO_SECRET_ENV = "MAESTRO_APPPILOT_INPUT_SECRET"

# How many characters to erase from a credential field before entering the
# secret, so repeated entries replace rather than append. Generous upper bound.
CREDENTIAL_FIELD_ERASE_CHARS = 128


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

    @property
    def selector_text(self) -> str:
        return self.text or self.accessibility_text or self.hint_text or self.label


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

    This is intentionally NOT part of :class:`DecisionRequest`: it is never
    included in prompts sent to the model, in the UI observation, in model
    responses, or in logs. The agent resolves a requested credential locally and
    passes the value directly to Maestro. Its ``repr`` deliberately hides values.
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


@dataclass(frozen=True)
class DecisionRequest:
    """Everything the decision model is given for a single step.

    The model chooses from ``available_actions`` only, so it can never invent an
    element that is not present in ``observation``.
    """

    goal: str
    guidance: str | None
    observation: UIObservation
    available_actions: tuple[Action, ...]
    context: ExecutionContext


@dataclass(frozen=True)
class ModelDecision:
    """The model's structured answer: one safe UI action, or cannot proceed.

    ``action is None`` means the model decided it cannot safely proceed. The
    model never executes anything; the agent validates and executes.
    """

    action: Action | None
    reason: str

    @property
    def can_proceed(self) -> bool:
        return self.action is not None


class ModelDecisionProvider(Protocol):
    """Boundary for a real, model-backed decision maker.

    Implementations turn a :class:`DecisionRequest` into a :class:`ModelDecision`.
    The concrete model is selected via configuration/environment so the agent
    loop never depends on a specific provider.
    """

    def decide(self, request: DecisionRequest) -> ModelDecision:
        ...


class GoalEvaluator(Protocol):
    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        ...


class MaestroHierarchyObserver:
    def __init__(self, device_id: str, max_elements: int = 100) -> None:
        self._device_id = device_id
        self._max_elements = max_elements

    def observe(self) -> UIObservation:
        command = [
            "maestro",
            "--no-ansi",
            "--udid",
            self._device_id,
            "hierarchy",
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Maestro hierarchy observation failed: {error}")

        try:
            hierarchy = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Maestro returned an invalid UI hierarchy") from error

        elements: list[UIElement] = []
        self._collect(hierarchy, (), None, False, elements)
        return UIObservation(tuple(elements[: self._max_elements]))

    def _collect(
        self,
        node: dict,
        path: tuple[int, ...],
        parent_id: str | None,
        in_system_ui: bool,
        elements: list[UIElement],
    ) -> list[str]:
        attributes = node.get("attributes", {})
        element_id = "e:" + (".".join(map(str, path)) if path else "root")
        resource_id = self._clean(attributes.get("resource-id"), limit=200)
        system_ui = in_system_ui or resource_id.startswith("com.android.systemui:")
        text = self._clean(attributes.get("text"))
        accessibility_text = self._clean(attributes.get("accessibilityText"))
        hint_text = self._clean(attributes.get("hintText"))
        class_name = self._clean(attributes.get("class"), limit=120)
        clickable = self._as_bool(attributes.get("clickable", node.get("clickable")))
        enabled = self._as_bool(attributes.get("enabled", node.get("enabled")), True)
        is_input = class_name.endswith("EditText") or "TextInput" in class_name

        # Redact credential fields: once a secret (e.g. a password) is typed, it
        # appears as this field's live text/accessibility value in the hierarchy.
        # Drop those value-bearing fields so the secret never reaches the
        # observation, label, model prompt, or execution trace. The stable hint
        # (e.g. "Password") is kept as a safe descriptor.
        field_credential_kind = (
            infer_credential_kind(resource_id, hint_text, class_name, accessibility_text)
            if is_input
            else None
        )
        if field_credential_kind is not None:
            text = ""
            accessibility_text = ""

        own_labels = [value for value in (text, accessibility_text, hint_text) if value]
        potentially_useful = bool(own_labels or clickable or is_input)
        child_parent_id = element_id if potentially_useful else parent_id
        child_labels: list[str] = []
        for index, child in enumerate(node.get("children", [])):
            child_labels.extend(
                self._collect(
                    child,
                    path + (index,),
                    child_parent_id,
                    system_ui,
                    elements,
                )
            )

        descendant_labels = self._unique(child_labels)[:3]
        label_parts = own_labels or (descendant_labels if clickable or is_input else [])
        label = " | ".join(self._unique(label_parts))

        useful = bool(label or clickable or is_input)
        if useful and not system_ui and len(elements) < self._max_elements:
            elements.append(
                UIElement(
                    element_id=element_id,
                    parent_id=parent_id,
                    text=text,
                    accessibility_text=accessibility_text,
                    hint_text=hint_text,
                    resource_id=resource_id,
                    class_name=class_name,
                    clickable=clickable,
                    enabled=enabled,
                    is_input=is_input,
                    label=label,
                )
            )

        return self._unique(own_labels + child_labels)[:8]

    @staticmethod
    def _clean(value: object, limit: int = 240) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:limit]

    @staticmethod
    def _as_bool(value: object, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == "true"
        return default

    @staticmethod
    def _unique(values: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))


class MicrosoftSignInGoalEvaluator:
    _TEXT_MARKERS = (
        "sign in with email or phone",
        "sign in to your account",
    )
    _RESOURCE_MARKERS = (
        "emailtextinput",
        "loginfmt",
        "i0116",
    )

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        del goal
        for element in observation.elements:
            visible_text = " ".join(
                (
                    element.text,
                    element.accessibility_text,
                    element.hint_text,
                    element.label,
                )
            ).casefold()
            resource_id = element.resource_id.casefold()
            if any(marker in visible_text for marker in self._TEXT_MARKERS):
                return True
            if element.is_input and any(
                marker in resource_id for marker in self._RESOURCE_MARKERS
            ):
                return True
        return False


class SignedInCopilotGoalEvaluator:
    """Deterministically recognizes a usable signed-in Microsoft 365 Copilot
    experience, independent of any decision provider.

    A usable signed-in Copilot state is one where the Copilot message composer is
    present and interactive. Two presentations are both valid PASS states:

    * the normal signed-in landing screen (empty composer, which may read
      "Message Copilot"); and
    * a deeplink-opened Copilot screen where a prompt is already populated in the
      composer (so "Message Copilot" is not necessarily shown).

    The introductory *random suggested-prompt* screen is NOT the goal: it must be
    dismissed via its close control rather than sent, so this evaluator does not
    report PASS while that introductory overlay is present. Likewise, a screen
    still showing a credential field is not signed in yet.
    """

    # Signals that a signed-in Copilot composer / home is present.
    _HOME_TEXT_MARKERS = ("message copilot",)
    _HOME_RESOURCE_MARKERS = ("home_screen", "copilot_composer", "copilot_input")
    _COMPOSER_HINT_MARKERS = (
        "message copilot",
        "ask copilot",
        "message m365 copilot",
        "message microsoft 365 copilot",
    )
    _COMPOSER_RESOURCE_HINTS = ("copilot", "composer")
    # Signals for the introductory suggested-prompt / onboarding screen that must
    # be dismissed (never treated as the final goal). "let's get started" is the
    # stable onboarding text shown on that intro screen (alongside a top-right X
    # and a composer pre-populated with a *suggested* prompt); it does not appear
    # on the real signed-in landing or a deeplink-opened Copilot chat, so it lets
    # AppPilot withhold PASS there and dismiss via the close control first.
    _INTRO_MARKERS = (
        "let's get started",
        "lets get started",
        "suggested prompt",
        "suggested_prompt",
        "suggestion_card",
        "prompt_starter",
        "intro_suggestion",
    )

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        del goal
        if self._has_credential_field(observation):
            return False
        if self._is_intro_suggested_prompt(observation):
            return False
        return self._has_signed_in_composer(observation)

    @staticmethod
    def _element_text(element: "UIElement") -> str:
        return " ".join(
            (
                element.text,
                element.accessibility_text,
                element.hint_text,
                element.label,
            )
        ).casefold()

    def _has_signed_in_composer(self, observation: UIObservation) -> bool:
        for element in observation.elements:
            text = self._element_text(element)
            resource_id = element.resource_id.casefold()
            if any(marker in text for marker in self._HOME_TEXT_MARKERS):
                return True
            if any(marker in resource_id for marker in self._HOME_RESOURCE_MARKERS):
                return True
            if element.is_input and (
                any(hint in resource_id for hint in self._COMPOSER_RESOURCE_HINTS)
                or any(marker in text for marker in self._COMPOSER_HINT_MARKERS)
            ):
                return True
        return False

    def _is_intro_suggested_prompt(self, observation: UIObservation) -> bool:
        for element in observation.elements:
            text = self._element_text(element)
            resource_id = element.resource_id.casefold()
            if any(
                marker in text or marker in resource_id for marker in self._INTRO_MARKERS
            ):
                return True
        return False

    @staticmethod
    def _has_credential_field(observation: UIObservation) -> bool:
        # Detect a login/credential field from stable field identity only
        # (resource id / hint / class). Deliberately excludes free content such
        # as a composer's pre-populated prompt, so a deeplink prompt that merely
        # mentions words like "email" is not misread as a sign-in field.
        for element in observation.elements:
            if not element.is_input:
                continue
            if infer_credential_kind(
                element.resource_id, element.hint_text, element.class_name
            ) is not None:
                return True
        return False


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


class UnconfiguredModelDecisionProvider:
    """Honest placeholder used when no decision model is configured.

    It makes no decisions and never guesses: every request returns "cannot
    safely proceed" with configuration guidance. This keeps the agent loop
    runnable without pretending to be intelligent.
    """

    def __init__(self, detail: str) -> None:
        self._detail = detail

    def decide(self, request: DecisionRequest) -> ModelDecision:
        del request
        return ModelDecision(
            action=None,
            reason=f"No decision model is configured, so AppPilot will not guess. {self._detail}",
        )


class LLMModelDecisionProvider:
    """Model-backed decision provider using an OpenAI-compatible chat API.

    The endpoint, model name and API key are read from the environment so that
    no provider, model, or credential is hardcoded in the orchestration logic:

    - ``APPPILOT_MODEL_API_KEY``  (required)
    - ``APPPILOT_MODEL``          (required, e.g. "gpt-4o-mini")
    - ``APPPILOT_MODEL_BASE_URL`` (optional, defaults to the OpenAI v1 endpoint)

    The model selects only from the safe actions supplied in the request, so it
    can never invent an element that is not in the current observation. The HTTP
    call is isolated in ``transport`` so it can be replaced or stubbed.
    """

    _SYSTEM_PROMPT = (
        "You are AppPilot, a goal-driven decision model that drives real Android "
        "apps through their UI. You are an autonomous agent, NOT a predefined test "
        "script and NOT a recording of a known flow.\n"
        "\n"
        "HOW APPPILOT WORKS:\n"
        "- You are given a GOAL. The GOAL is the ultimate objective; everything "
        "you decide must move toward it.\n"
        "- On each turn you receive ONLY the CURRENT observed UI (the elements on "
        "screen right now) and a numbered list of safe actions AppPilot is "
        "allowed to take.\n"
        "- You decide EXACTLY ONE next action from that current observation. You "
        "do not plan or assume a fixed A->B->C path, and you do not assume which "
        "screen comes next.\n"
        "- After the action runs, the UI is observed again and you are asked for a "
        "fresh decision from the new observation. Progress happens one observed "
        "step at a time.\n"
        "\n"
        "REASONING PRINCIPLES:\n"
        "- Treat the CURRENT observation as the only ground truth. Any screen can "
        "appear at any time; you can never enumerate them all in advance, so do "
        "not rely on a memorized list of known screens or buttons. Reason from "
        "what is actually on screen now, and never fail merely because a screen "
        "was not predicted.\n"
        "- For every screen, first classify it against the GOAL: does acting on "
        "it move toward the goal, or is it an incidental INTERRUPTION that is "
        "unrelated to the goal? Interruptions are common and take many forms "
        "(system or app dialogs, error/warning/crash notices, permission or "
        "consent prompts, 'save password' or update offers, surveys, ratings, "
        "tips, ads, and others not listed here). This list is illustrative and "
        "NOT exhaustive; judge by intent, not by matching specific words.\n"
        "- To clear an unrelated interruption, pick the safest available control "
        "that closes or declines it and returns you toward the goal. Prefer "
        "neutral/dismissive controls (such as OK, Close, Cancel, Dismiss, Not "
        "now, Skip, No thanks, Later, or a back/close affordance) over ones that "
        "commit to something. If several safe options exist, choose the one least "
        "likely to change app state. Only after clearing the interruption do you "
        "resume goal-directed actions.\n"
        "- Never choose an option that agrees to, buys, deletes, resets, "
        "overwrites, signs out, or changes account/security/permission settings "
        "just to get past an interruption. If the ONLY way past a screen requires "
        "such an action, do not take it: return action_id=null and explain that "
        "no safe way forward is available.\n"
        "- Prefer safe, non-destructive actions that clearly advance the goal.\n"
        "- If your previous action did not produce the transition you expected, "
        "re-read the NEWLY observed UI and reason from it. Do not blindly repeat a "
        "scripted step or the same action that just failed.\n"
        "- Optimize for progress toward the GOAL, not for reproducing any known or "
        "historical flow.\n"
        "- Do NOT submit, send, or execute a text prompt just because one is "
        "visible or already typed in a composer. Only send a prompt when the GOAL "
        "explicitly requires submitting it. An introductory screen offering a "
        "random suggested prompt (with a Send button and a close/X control) is an "
        "interruption: close it rather than sending the suggestion. A prompt that "
        "is already populated in the composer may simply be left as-is.\n"
        "\n"
        "HARD RULES:\n"
        "- Choose ONLY an action from the provided list, by its id. NEVER invent "
        "elements, labels, resource ids, screen coordinates, or application state "
        "that is not in the current observation.\n"
        "- CREDENTIALS ARE SPECIAL. Some input actions are credential fields (a "
        "username/email field or a password field). For those you must NOT ask "
        "for, guess, reproduce, or output any secret value. Set \"input_kind\" to "
        "\"username\" or \"password\" and AppPilot supplies the real value "
        "securely; you never see or generate credentials, and you must never put a "
        "credential value in \"text\" or in your \"reason\".\n"
        "- For non-credential input fields, provide the literal \"text\".\n"
        "- GOAL EVALUATION IS NOT YOURS TO DECLARE. AppPilot decides PASS "
        "deterministically. If the GOAL already appears satisfied by the current "
        "UI, do NOT take another action: return action_id=null and explain in "
        "\"reason\" that the goal looks reached. Likewise return action_id=null "
        "when no listed action can safely progress the goal.\n"
        "\n"
        "RESPONSE FORMAT (strict JSON, no prose outside the JSON):\n"
        '{"action_id": <int|null>, "text": <string|null>, '
        '"input_kind": <"username"|"password"|null>, "reason": <string>}'
    )

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        transport: Callable[[dict], dict] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport or self._http_transport
        self._timeout = timeout

    @classmethod
    def from_env(
        cls, env: dict | None = None
    ) -> "LLMModelDecisionProvider | None":
        env = os.environ if env is None else env
        api_key = env.get("APPPILOT_MODEL_API_KEY")
        model = env.get("APPPILOT_MODEL")
        if not api_key or not model:
            return None
        base_url = env.get("APPPILOT_MODEL_BASE_URL") or "https://api.openai.com/v1"
        return cls(model=model, api_key=api_key, base_url=base_url)

    def decide(self, request: DecisionRequest) -> ModelDecision:
        options = list(request.available_actions)
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": self._render_request(request, options)},
            ],
            "response_format": {"type": "json_object"},
        }
        response = self._transport(payload)
        try:
            content = response["choices"][0]["message"]["content"]
            decoded = json.loads(content)
        except (KeyError, IndexError, ValueError, TypeError) as error:
            return ModelDecision(
                action=None, reason=f"Model response could not be parsed: {error}"
            )
        return self._interpret(decoded, options)

    def _interpret(self, decoded: dict, options: list[Action]) -> ModelDecision:
        action_id = decoded.get("action_id")
        reason = str(decoded.get("reason") or "").strip() or "(no reason given)"
        if action_id is None:
            return ModelDecision(action=None, reason=reason)
        if not isinstance(action_id, int) or not 0 <= action_id < len(options):
            return ModelDecision(
                action=None,
                reason=(
                    f"Model chose action_id {action_id!r}, which is not one of the "
                    "offered safe actions."
                ),
            )
        chosen = options[action_id]
        if chosen.kind == ActionKind.INPUT_TEXT:
            if chosen.credential_kind is not None:
                requested = decoded.get("input_kind")
                if not isinstance(requested, str) or requested.strip().casefold() not in (
                    CredentialKind.USERNAME.value,
                    CredentialKind.PASSWORD.value,
                ):
                    return ModelDecision(
                        action=None,
                        reason=(
                            "Model selected a credential input but did not request a "
                            'valid input_kind ("username" or "password").'
                        ),
                    )
                if requested.strip().casefold() != chosen.credential_kind.value:
                    return ModelDecision(
                        action=None,
                        reason=(
                            f"Model requested input_kind {requested!r} but the field is "
                            f"a {chosen.credential_kind.value} field."
                        ),
                    )
                # Keep credential_kind; input_text stays None so the model never
                # sees or produces the secret. The agent resolves it locally.
            else:
                text = decoded.get("text")
                if not isinstance(text, str) or not text:
                    return ModelDecision(
                        action=None,
                        reason="Model selected an input action but supplied no text.",
                    )
                chosen = Action(
                    ActionKind.INPUT_TEXT, target_id=chosen.target_id, input_text=text
                )
        return ModelDecision(action=chosen, reason=reason)

    def _render_request(self, request: DecisionRequest, options: list[Action]) -> str:
        lines = [f"GOAL: {request.goal}"]
        if request.guidance:
            lines.append(f"GUIDANCE: {request.guidance}")
        context = request.context
        lines.append(f"STEP: {context.step} of {context.max_steps}")
        if context.history:
            lines.append("RECENT ACTIONS:")
            lines.extend(f"  - {item}" for item in context.history)
        lines.append("CURRENT UI ELEMENTS:")
        lines.append(request.observation.describe(limit=40))
        lines.append("AVAILABLE SAFE ACTIONS:")
        for index, action in enumerate(options):
            lines.append(
                f"  {index}: {self._describe_action(action, request.observation)}"
            )
        lines.append(
            "Respond with JSON choosing one action id, or action_id=null if you "
            "cannot safely proceed."
        )
        return "\n".join(lines)

    @staticmethod
    def _describe_action(action: Action, observation: UIObservation) -> str:
        if action.kind == ActionKind.PRESS_BACK:
            return "press back"
        target = observation.find(action.target_id)
        label = target.label if target else action.target_id
        if action.kind == ActionKind.INPUT_TEXT:
            if action.credential_kind is not None:
                # Safe descriptor only; never expose the field's live value.
                descriptor = (
                    (target.resource_id or target.hint_text or action.target_id)
                    if target is not None
                    else action.target_id
                )
                return (
                    f'input the {action.credential_kind.value} into {descriptor} '
                    f'(set input_kind="{action.credential_kind.value}"; AppPilot '
                    "supplies the value securely)"
                )
            return f'input text into "{label}" (provide text)'
        return f'tap "{label}"'

    def _http_transport(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as error:
            raise RuntimeError(f"Model request failed: {error}") from error


class MaestroExecutor:
    def __init__(self, app_id: str, device_id: str) -> None:
        self._app_id = app_id
        self._device_id = device_id

    def execute(
        self,
        action: Action,
        observation: UIObservation,
        secret: str | None = None,
    ) -> None:
        commands = self._commands_for(action, observation, secret is not None)
        flow = f"appId: {self._app_id}\n---\n{commands}"
        # For credential inputs the secret is never written to the flow file; it
        # is passed to Maestro through a MAESTRO_-prefixed environment variable
        # and interpolated as ${...}. This keeps it out of the YAML, argv and logs.
        run_env: dict[str, str] | None = None
        if secret is not None:
            run_env = {**os.environ, MAESTRO_SECRET_ENV: secret}
        flow_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".yaml",
                prefix="apppilot-action-",
                delete=False,
            ) as flow_file:
                flow_file.write(flow)
                flow_path = Path(flow_file.name)

            result = subprocess.run(
                [
                    "maestro",
                    "--no-ansi",
                    "--udid",
                    self._device_id,
                    "test",
                    str(flow_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
                env=run_env,
            )
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
                # Never let a resolved secret surface via an error message, even
                # if Maestro echoes the interpolated value in its output.
                if secret:
                    error = error.replace(secret, "***")
                raise RuntimeError(f"Maestro action execution failed: {error}")
        finally:
            if flow_path:
                flow_path.unlink(missing_ok=True)

    def _commands_for(
        self,
        action: Action,
        observation: UIObservation,
        use_secret: bool,
    ) -> str:
        if action.kind == ActionKind.PRESS_BACK:
            return "- pressKey: BACK\n"

        target = observation.find(action.target_id)
        if target is None:
            raise ValueError("Cannot execute an action without an observed target")
        selector = self._selector(target)

        if action.kind == ActionKind.TAP:
            return f"- tapOn:\n{selector}"
        if action.kind == ActionKind.INPUT_TEXT:
            if action.credential_kind is not None or use_secret:
                # Clear any existing/accumulated content first so repeated entry
                # replaces rather than appends, then inject the secret via the
                # environment placeholder (never the literal value in the YAML).
                return (
                    f"- tapOn:\n{selector}"
                    f"- eraseText: {CREDENTIAL_FIELD_ERASE_CHARS}\n"
                    f"- inputText: ${{{MAESTRO_SECRET_ENV}}}\n"
                )
            value = json.dumps(action.input_text)
            return f"- tapOn:\n{selector}- inputText: {value}\n"
        raise ValueError(f"Unsupported action kind: {action.kind}")

    @staticmethod
    def _selector(target: UIElement) -> str:
        if target.resource_id:
            return f"    id: {json.dumps(target.resource_id)}\n"
        if target.selector_text:
            return f"    text: {json.dumps(target.selector_text)}\n"
        raise ValueError("Observed target has no safe Maestro selector")


class AppPilotAgent:
    def __init__(
        self,
        observer: MaestroHierarchyObserver,
        goal_evaluator: GoalEvaluator,
        decision_provider: ModelDecisionProvider,
        safety_validator: SafetyValidator,
        executor: MaestroExecutor,
        max_actions: int,
        runtime_context: RuntimeContext,
        max_stuck_actions: int = DEFAULT_MAX_STUCK_ACTIONS,
    ) -> None:
        self._observer = observer
        self._goal_evaluator = goal_evaluator
        self._decision_provider = decision_provider
        self._safety_validator = safety_validator
        self._executor = executor
        self._max_actions = max_actions
        self._runtime_context = runtime_context
        self._max_stuck_actions = max_stuck_actions

    def run(self, goal: str, guidance: str | None = None) -> bool:
        print(f"GOAL:\n{goal}\n")
        if guidance:
            print(f"GUIDANCE:\n{guidance}\n")
        print(f"MAX ACTIONS:\n{self._max_actions}\n")
        print(f"MAX STUCK ACTIONS:\n{self._max_stuck_actions}\n")

        history: list[str] = []
        # Track the last credential we entered and the screen it was entered on,
        # to avoid re-entering the same credential when the UI has not changed.
        last_credential_key: tuple[str, str] | None = None
        last_credential_fingerprint: tuple | None = None
        # Track consecutive actions that leave the meaningful UI unchanged.
        last_acted_fingerprint: tuple | None = None
        consecutive_stuck = 0
        for step in range(self._max_actions + 1):
            observation = self._observer.observe()
            print(f"OBSERVE:\n{observation.describe()}\n")

            reached = self._goal_evaluator.is_reached(goal, observation)
            print(f"GOAL REACHED:\n{str(reached).lower()}\n")
            if reached:
                print("RESULT:\nPASS")
                return True

            # Advance the stuck counter when the last action left the meaningful
            # UI unchanged; a meaningful change resets it. Only counts once an
            # action has been taken (last_acted_fingerprint is set).
            meaningful_fingerprint = self._meaningful_fingerprint(observation)
            if last_acted_fingerprint is not None:
                if meaningful_fingerprint == last_acted_fingerprint:
                    consecutive_stuck += 1
                else:
                    consecutive_stuck = 0
            print(f"PROGRESS:\nstuck {consecutive_stuck}/{self._max_stuck_actions}\n")
            if consecutive_stuck >= self._max_stuck_actions:
                print(
                    "RESULT:\nFAIL - agent appears stuck: no meaningful UI change "
                    f"for {consecutive_stuck} consecutive actions"
                )
                return False

            if step == self._max_actions:
                print(f"RESULT:\nFAIL - action/step limit reached ({self._max_actions})")
                return False

            available_actions = self._safety_validator.available_actions(observation)
            request = DecisionRequest(
                goal=goal,
                guidance=guidance,
                observation=observation,
                available_actions=available_actions,
                context=ExecutionContext(
                    step=step, max_steps=self._max_actions, history=tuple(history)
                ),
            )
            # The model is the decision-maker; the agent only asks and validates.
            decision = self._decision_provider.decide(request)

            if decision.action is None:
                print(
                    "MODEL DECISION:\ncannot safely proceed\n"
                    f"Reason: {decision.reason}\n"
                )
                print("RESULT:\nFAIL - model cannot safely proceed")
                return False

            print(
                f"MODEL DECISION:\n{decision.action.describe(observation)}\n"
                f"Reason: {decision.reason}\n"
            )

            try:
                self._safety_validator.validate(decision.action, observation)
            except ValueError as error:
                print(f"SAFETY VALIDATION:\nrejected - {error}\n")
                print("RESULT:\nFAIL - model proposed an unsafe or invalid action")
                return False
            print("SAFETY VALIDATION:\npassed\n")

            # Resolve any requested credential locally, after safety validation.
            # The secret value is never printed and never leaves this scope
            # except to be handed directly to Maestro.
            secret: str | None = None
            action = decision.action
            if action.credential_kind is not None:
                if not self._runtime_context.has(action.credential_kind):
                    print(
                        "CREDENTIAL:\nrequired "
                        f"{action.credential_kind.value} is not configured\n"
                    )
                    print(
                        "RESULT:\nFAIL - required credential is not configured "
                        f"({action.credential_kind.value})"
                    )
                    return False

                # Guard against re-entering the same credential on an unchanged
                # screen (prevents redundant entry loops). The model remains the
                # decision-maker; this is only a safety guard, not a login step.
                target = observation.find(action.target_id)
                credential_key = (
                    action.credential_kind.value,
                    (target.resource_id if target and target.resource_id else action.target_id)
                    or action.target_id
                    or "",
                )
                fingerprint = self._observation_fingerprint(observation)
                if (
                    credential_key == last_credential_key
                    and fingerprint == last_credential_fingerprint
                ):
                    print(
                        "CREDENTIAL:\n"
                        f"{action.credential_kind.value} already entered on this "
                        "screen; UI unchanged\n"
                    )
                    print(
                        "RESULT:\nFAIL - repeated credential entry with no UI "
                        "change (possible loop)"
                    )
                    return False

                secret = self._runtime_context.resolve(action.credential_kind)
                last_credential_key = credential_key
                last_credential_fingerprint = fingerprint

            print(f"ACTION:\n{action.describe(observation)}\n")
            self._executor.execute(action, observation, secret=secret)
            history.append(action.describe(observation))
            # Remember the state we just acted on, to detect progress next step.
            last_acted_fingerprint = meaningful_fingerprint

        raise AssertionError("Agent loop exited unexpectedly")

    @staticmethod
    def _observation_fingerprint(observation: UIObservation) -> tuple:
        """A stable signature of the screen using non-secret element traits.

        Credential-field values are already redacted by the observer, so this
        never incorporates a secret. Used to detect whether the UI meaningfully
        changed between steps.
        """
        return tuple(
            (
                element.resource_id,
                element.label,
                element.clickable,
                element.is_input,
                element.enabled,
            )
            for element in observation.elements
        )

    @staticmethod
    def _meaningful_fingerprint(observation: UIObservation) -> tuple:
        """A stable signature of the meaningful UI state, for stuck detection.

        Non-secret, like ``_observation_fingerprint``, but limited to elements
        with a resource id or that are interactive (clickable/input). Decorative,
        id-less, non-interactive text (volatile clocks/animation) is excluded, so
        such noise does not reset the stuck counter.
        """
        return tuple(
            (
                element.resource_id,
                element.label,
                element.clickable,
                element.is_input,
                element.enabled,
            )
            for element in observation.elements
            if element.resource_id or element.clickable or element.is_input
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the AppPilot goal-driven Android agent prototype."
    )
    parser.add_argument("--device", default="emulator-5554")
    parser.add_argument(
        "--max-actions",
        type=int,
        default=_default_max_actions(),
        help="Safety bound on actions per run (default: env APPPILOT_MAX_ACTIONS "
        f"or {DEFAULT_MAX_ACTIONS}).",
    )
    parser.add_argument(
        "--max-stuck-actions",
        type=int,
        default=_default_max_stuck_actions(),
        help="Consecutive actions with no meaningful UI change before the run is "
        "stopped early (default: env APPPILOT_MAX_STUCK_ACTIONS or "
        f"{DEFAULT_MAX_STUCK_ACTIONS}).",
    )
    parser.add_argument("--guidance", default=DEFAULT_GUIDANCE)
    return parser.parse_args()


def _default_max_actions() -> int:
    """Resolve the default action bound from the environment, else the constant."""
    raw = os.environ.get("APPPILOT_MAX_ACTIONS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_ACTIONS


def _default_max_stuck_actions() -> int:
    """Resolve the stuck bound from the environment, else the constant.

    Precedence: --max-stuck-actions -> APPPILOT_MAX_STUCK_ACTIONS -> constant;
    invalid environment values fall back safely.
    """
    raw = os.environ.get("APPPILOT_MAX_STUCK_ACTIONS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_STUCK_ACTIONS


def _load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a local .env into os.environ, if present.

    Existing environment variables always win, so real env/CI values are never
    overridden. No secret is hardcoded here; the file is git-ignored and only
    read at runtime. Lines that are blank or start with '#' are ignored.
    """
    env_path = path or (REPO_ROOT / ".env")
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    # Load the local, git-ignored .env first so values such as the model
    # credentials and APPPILOT_MAX_ACTIONS are available before args are parsed.
    # Real environment variables always take precedence.
    _load_dotenv()

    args = _parse_args()
    if args.max_actions < 1:
        print("ERROR: --max-actions must be at least 1", file=sys.stderr)
        return 2
    if args.max_stuck_actions < 1:
        print("ERROR: --max-stuck-actions must be at least 1", file=sys.stderr)
        return 2

    provider: ModelDecisionProvider | None = LLMModelDecisionProvider.from_env()
    if provider is None:
        provider = UnconfiguredModelDecisionProvider(
            "Set APPPILOT_MODEL and APPPILOT_MODEL_API_KEY (and optionally "
            "APPPILOT_MODEL_BASE_URL) to connect a real decision model."
        )

    agent = AppPilotAgent(
        observer=MaestroHierarchyObserver(args.device),
        goal_evaluator=SignedInCopilotGoalEvaluator(),
        decision_provider=provider,
        safety_validator=SafetyValidator(),
        executor=MaestroExecutor(APP_ID, args.device),
        max_actions=args.max_actions,
        runtime_context=RuntimeContext.from_env(),
        max_stuck_actions=args.max_stuck_actions,
    )
    try:
        return 0 if agent.run(PROTOTYPE_GOAL, args.guidance) else 1
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"RESULT:\nFAIL - {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
