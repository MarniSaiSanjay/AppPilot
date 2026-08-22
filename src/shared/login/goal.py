"""Shared login goal evaluators (use-case-agnostic).

The generic authentication/onboarding boundary detectors used by the shared
login node: a deterministic Copilot terminal detector, the model-backed
login-completion judge, the authoritative composition of the two, and a generic
semantic screen-state judge used by SemanticTerminalState. Nothing here knows
about Deeplink, FRI, or any specific use case.
"""

from __future__ import annotations

import json
from typing import Callable

try:  # package-relative (python -m src.shared.login.goal) vs top-level (src on path)
    from ...apppilot.models import UIElement, UIObservation
    from ...apppilot.safety import infer_credential_kind
    from ..model_client import ChatModelClient, ModelTransportError, DEFAULT_BASE_URL
except ImportError:
    from apppilot.models import UIElement, UIObservation
    from apppilot.safety import infer_credential_kind
    from shared.model_client import ChatModelClient, ModelTransportError, DEFAULT_BASE_URL



# The agent reasons over the observed UI and drives Maestro action-by-action;
# it does not execute any prewritten Maestro flow.
PROTOTYPE_GOAL = (
    "Complete authentication and the minimum required first-launch onboarding. "
    "Once the initial suggested-prompt/welcome interruption has been dismissed "
    "(or was never shown because the app is already signed in), login is "
    "complete: stop and return control to the caller. Do NOT navigate further "
    "into Copilot, do NOT tap suggested actions or 'Ask Copilot', and do NOT "
    "open files or send prompts to prove Copilot is usable."
)
DEFAULT_GUIDANCE = (
    "Advance ONLY through sign-in and required first-launch onboarding. When a "
    "sign-in email/username or password field is shown, choose the matching "
    "credential input action and set input_kind accordingly; AppPilot supplies "
    "the actual value securely, so never type a credential yourself. Enter the "
    "password into the actual editable password INPUT field only - never the "
    "show/hide (view) password icon, the password label/hint, a 'Next'/'Sign "
    "in' button, or any validation/error text. PREFER "
    "PASSWORD SIGN-IN: if the screen offers to send or verify a one-time code "
    "(for example a code sent to a phone/SMS/text or email, an authenticator "
    "approval, or any passwordless/verification-code screen), do NOT request or "
    "wait for a code. Instead choose an alternative like 'Other ways to sign "
    "in', 'Sign in another way', 'Use your password', or 'Use password "
    "instead', then select the PASSWORD method and enter the password to "
    "continue. Only fall back to a code method if no password option exists at "
    "all. Dismiss "
    "incidental interruptions (for example a 'Save password' prompt) with the "
    "safest non-destructive option and keep going. If the introductory screen "
    "shows a random suggested prompt with a Send button and a close (X) control, "
    "close it with the X instead of sending the suggestion - that dismissal "
    "completes onboarding. Do NOT continue into the app afterwards: once past "
    "sign-in and that initial interruption, take no further actions - the next "
    "screen belongs to the caller, not to login."
)



class SignedInCopilotGoalEvaluator:
    """Decides when the shared login flow has PREPARED the app and must stop.

    Login is preparation, not navigation: get past auth and any onboarding
    present, then hand the current screen to the caller. One stateless rule:

        login complete  <=>  no login BLOCKER present  AND  SETTLED inside

    * BLOCKER: an auth surface (credential field / sign-in affordance) or the
      optional intro/suggested-prompt interruption (dismiss if shown; its
      absence never blocks completion).
    * SETTLED inside: a recognizable Copilot home/composer, or any actionable app
      screen. Nothing to act on = transient loading (wait, don't press Back).

    Screens are matched against marker tables (data), so extending recognition
    edits a table, not a branch.
    """

    def __init__(
        self, foreground_check: "Callable[[], bool] | None" = None
    ) -> None:
        # Optional deterministic "is the target app foreground?" check. When
        # supplied (e.g. by the deeplink flow), login is never reported complete
        # while the target app is not foreground. None for standalone use.
        self._foreground_check = foreground_check

    # Recognizable signed-in Copilot home/composer.
    _HOME_TEXT = ("message copilot",)
    _HOME_RESOURCE = ("home_screen", "copilot_composer", "copilot_input")
    # Composer hints that count only on an input field (a text field, not a label).
    _COMPOSER_INPUT_TEXT = (
        "message copilot",
        "ask copilot",
        "message m365 copilot",
        "message microsoft 365 copilot",
    )
    _COMPOSER_INPUT_RESOURCE = ("copilot", "composer")
    _SEARCH_INPUT_TEXT = ("search",)
    _SEARCH_INPUT_RESOURCE = ("search_box", "search_input", "search_field")
    # Explicit negative authentication terminals: restricted/denied access,
    # blocked/disabled accounts and outright sign-in failures. These are NOT a
    # successful login - the login form merely disappearing behind such a screen
    # must never be reported as reached. This is only a lean deterministic
    # SAFETY NET for the explicit, common negatives (and the no-AI fallback
    # path); the semantic judge handles novel wordings. Generic multi-word
    # English phrases whose negative sense is carried by the paired word (e.g.
    # "denied"/"restricted"/"failed"), so a legitimate screen merely mentioning
    # "access", "permission" or "account" does not match. App-agnostic.
    _NEGATIVE_AUTH_TEXT = (
        "not eligible",
        "access denied",
        "access restricted",
        "restricted access",
        "permission denied",
        "account restricted",
        "account blocked",
        "account is blocked",
        "account disabled",
        "sign-in failed",
        "sign in failed",
        "unable to sign in",
        "couldn't sign you in",
        "authentication failed",
        "not authorized",
        "password is incorrect",
        "incorrect password",
        "wrong password",
        "invalid password",
        "username or password is incorrect",
        "account or password is incorrect",
    )
    _NEGATIVE_AUTH_RESOURCE = (
        "access_denied",
        "access_restricted",
        "account_blocked",
        "signin_error",
    )
    # Optional intro/suggested-prompt interruption to dismiss when present.
    _INTRO = (
        "let's get started",
        "lets get started",
        "suggested prompt",
        "suggested_prompt",
        "suggestion_card",
        "prompt_starter",
        "intro_suggestion",
    )
    # Sign-in affordances. Kept specific so an authenticated screen is not
    # misread. Generic auth patterns (incl. the federated "Continue with
    # <provider>" landing), not app-specific.
    _SIGNIN = (
        "sign in",
        "signin",
        "sign-in",
        "sign up",
        "signup",
        "sign-up",
        "log in",
        "login",
        "log-in",
        "continue with",
        "use another account",
        "pick an account",
        "choose an account",
        "add account",
    )

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        del goal
        verdict = self.deterministic_verdict(observation)
        if verdict is not None:
            return verdict
        # Preserve the legacy fallback for standalone/offline callers. Production
        # wraps this evaluator and sends ambiguous screens to the semantic judge.
        return self._settled_inside(observation)

    def deterministic_verdict(
        self, observation: UIObservation
    ) -> "bool | None":
        """Return a definite login verdict, or None for an ambiguous app screen."""
        if self._foreground_check is not None and not self._foreground_check():
            return False
        if self._blocked(observation):
            return False
        # An explicit restricted/denied/failed authentication screen is a
        # NEGATIVE terminal: login definitively did not succeed. Decide it
        # deterministically as not-reached so it can never be mistaken for a
        # usable in-app screen (and never reach the semantic judge to be flipped).
        if self._negative_auth_terminal(observation):
            return False
        if (
            self._signed_in_home(observation)
            or self._search_screen(observation)
        ):
            return True
        if not self._has_actionable_ui(observation):
            return False
        return None

    def deterministic_actionable_verdict(
        self, observation: UIObservation
    ) -> "bool | None":
        """Return whether a definite login step exists, or None if ambiguous."""
        if self._foreground_check is not None and not self._foreground_check():
            return False
        if self._blocked(observation):
            return True
        # A negative-auth terminal offers no login/onboarding step to act on: the
        # agent should stop driving and reach a bounded, controlled non-PASS
        # rather than hand the dead-end screen to the Brain.
        if self._negative_auth_terminal(observation):
            return False
        if (
            self._signed_in_home(observation)
            or self._search_screen(observation)
        ):
            return True
        if not self._has_actionable_ui(observation):
            return False
        return None

    def _blocked(self, observation: UIObservation) -> bool:
        # Not yet past authentication + onboarding.
        return self._authenticating(observation) or self._intro_present(observation)

    def has_actionable_step(self, observation: UIObservation) -> bool:
        """Whether this screen presents a genuine login/onboarding step to act on.

        Used by the agent's wait-gate to tell a real authentication/onboarding
        screen (a sign-in affordance, a credential field, or the optional intro
        interruption to dismiss) apart from a transient loading/splash screen that
        merely carries an incidental clickable element (e.g. a "Terms of use" link
        on a "looking for accounts" screen). The terminal, signed-in states are
        handled by ``is_reached`` returning True before the wait-gate runs, so
        here a step exists iff the screen is a login BLOCKER. If the target app is
        not yet foreground, there is nothing to act on - wait."""
        if self._foreground_check is not None and not self._foreground_check():
            return False
        return self._blocked(observation)

    def failure_reason(self, observation: UIObservation) -> "str | None":
        """Explain a definitive authentication rejection without retrying it."""
        if not self._negative_auth_terminal(observation):
            return None
        return (
            "authentication was rejected by the sign-in service; verify "
            "APPPILOT_USERNAME and APPPILOT_PASSWORD"
        )

    def _settled_inside(self, observation: UIObservation) -> bool:
        # Positive evidence we are in the authenticated app (not mid-transition).
        return self._signed_in_home(observation) or self._has_actionable_ui(observation)

    def _authenticating(self, observation: UIObservation) -> bool:
        return (
            self._has_credential_field(observation)
            or self._matches(observation, text=self._SIGNIN, resource=self._SIGNIN)
        )

    def _intro_present(self, observation: UIObservation) -> bool:
        return self._matches(observation, text=self._INTRO, resource=self._INTRO)

    def _signed_in_home(self, observation: UIObservation) -> bool:
        return self._matches(
            observation, text=self._HOME_TEXT, resource=self._HOME_RESOURCE
        ) or self._matches(
            observation,
            text=self._COMPOSER_INPUT_TEXT,
            resource=self._COMPOSER_INPUT_RESOURCE,
            require_input=True,
        )

    def _search_screen(self, observation: UIObservation) -> bool:
        return self._matches(
            observation,
            text=self._SEARCH_INPUT_TEXT,
            resource=self._SEARCH_INPUT_RESOURCE,
            require_input=True,
        )

    def _negative_auth_terminal(self, observation: UIObservation) -> bool:
        return self._matches(
            observation,
            text=self._NEGATIVE_AUTH_TEXT,
            resource=self._NEGATIVE_AUTH_RESOURCE,
        )

    @staticmethod
    def _has_actionable_ui(observation: UIObservation) -> bool:
        # Same "actionable" definition as SafetyValidator/the agent's loading wait.
        return any(
            (element.clickable or element.is_input)
            and (element.resource_id or element.selector_text)
            for element in observation.elements
        )

    @classmethod
    def _matches(
        cls,
        observation: UIObservation,
        *,
        text: tuple[str, ...] = (),
        resource: tuple[str, ...] = (),
        require_input: bool = False,
    ) -> bool:
        """True if any element's text/resource-id contains one of the markers."""
        for element in observation.elements:
            if require_input and not element.is_input:
                continue
            element_text = cls._element_text(element)
            resource_id = element.resource_id.casefold()
            if any(marker in element_text for marker in text):
                return True
            if any(marker in resource_id for marker in resource):
                return True
        return False

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

    @staticmethod
    def _has_credential_field(observation: UIObservation) -> bool:
        # Match on stable field identity (resource id / hint / class) only, so a
        # composer prompt merely mentioning "email" is not misread as a field.
        for element in observation.elements:
            if not element.is_input:
                continue
            if infer_credential_kind(
                element.resource_id, element.hint_text, element.class_name
            ) is not None:
                return True
        return False


class LLMLoginGoalEvaluator:
    """Model-backed login-completion judge: the AI reads the WHOLE screen.

    Instead of matching fixed app-specific strings, asks the same model the agent
    uses whether the user has finished authentication + any required onboarding
    and is now INSIDE the app (not on a sign-in/welcome/account-picker/consent
    screen, the optional suggested-prompt interruption, or a loading screen).

    The only non-AI part is the deterministic foreground precondition (adb):
    login is never complete while the app is not foreground. The model judges
    only the in-app-vs-auth/onboarding distinction, by meaning - robust to
    wording/layout changes, needing no hardcoded labels.
    """

    _SYSTEM_PROMPT = (
        "You are AppPilot's login-completion judge for an Android app. You are "
        "given the app's GOAL for the login/onboarding step and the CURRENT "
        "observed Android UI. The target app is already in the foreground.\n"
        "\n"
        "Read the WHOLE screen and judge by MEANING - never rely on a single "
        "hardcoded label. Return TWO booleans.\n"
        "\n"
        "1) reached: has the user finished signing in AND any required "
        "first-launch onboarding, so control should be handed back to the "
        "caller?\n"
        "   - reached=false when the screen is any authentication or pre-app "
        "screen, for example: a sign-in / log-in / sign-up / welcome / landing "
        "screen (including 'Continue with <provider>' buttons), an account "
        "picker or consent/permission prompt, an email or password field, an "
        "MFA/verification prompt, OR the optional initial suggested-prompt / "
        "welcome / onboarding interruption that should be dismissed first, OR a "
        "transient loading / splash / 'looking for account' screen that has not "
        "settled yet.\n"
        "   - reached=true ONLY when the screen is clearly a normal, usable "
        "IN-APP screen that belongs to the signed-in user (for example a home / "
        "chat / composer / search / content screen) with no sign-in affordance "
        "and no pending onboarding interruption.\n"
        "\n"
        "2) actionable_step: is there a CONCRETE sign-in / authentication / "
        "onboarding control that the automation should act on RIGHT NOW - for "
        "example a sign-in or 'Continue with <provider>' button, an account to "
        "pick, a visible email/password field, or an intro/suggested-prompt to "
        "dismiss?\n"
        "   - actionable_step=false when the screen is a transient loading / "
        "splash / 'looking for accounts' / syncing / fetching state that has no "
        "such control yet, EVEN IF it carries incidental links (like a 'Terms of "
        "use' or 'Privacy' link). On such a screen the automation must WAIT for "
        "the real control to appear rather than pressing Back to hunt for it.\n"
        "   - actionable_step=true when a real login/onboarding control is "
        "present to interact with.\n"
        "\n"
        "When unsure or the screen is ambiguous/mid-transition, prefer "
        "reached=false; and if no concrete login control is visible yet, prefer "
        "actionable_step=false so the agent waits rather than acting blindly.\n"
        "\n"
        "RESPOND with strict JSON only, no prose outside it:\n"
        '{"reached": <true|false>, "actionable_step": <true|false>, '
        '"reason": <string>}'
    )

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        *,
        foreground_check: "Callable[[], bool] | None" = None,
        transport: "Callable[[dict], dict] | None" = None,
        timeout: float = 60.0,
    ) -> None:
        self._client = ChatModelClient(
            model=model, api_key=api_key, base_url=base_url, timeout=timeout
        )
        self._model = model
        self._foreground_check = foreground_check
        self._transport = transport or self._http_transport
        # 1-entry cache so is_reached() and has_actionable_step(), which the agent
        # calls on the SAME observation each step, share ONE model call. Keyed by
        # a stable, secret-free fingerprint of the screen.
        self._cache_key: tuple | None = None
        self._cache_val: dict | None = None

    def begin_run(self) -> None:
        self._cache_key = None
        self._cache_val = None

    @classmethod
    def from_env(
        cls,
        env: dict | None = None,
        *,
        foreground_check: "Callable[[], bool] | None" = None,
        transport: "Callable[[dict], dict] | None" = None,
    ) -> "LLMLoginGoalEvaluator | None":
        config = ChatModelClient.config_from_env(env)
        if config is None:
            return None
        return cls(
            model=config["model"],
            api_key=config["api_key"],
            base_url=config["base_url"],
            foreground_check=foreground_check,
            transport=transport,
        )

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        # Deterministic precondition: never "logged in" while the target app is
        # not foreground (e.g. the store window is still up). Not an AI decision.
        if self._foreground_check is not None and not self._foreground_check():
            return False
        return bool(self._evaluate(goal, observation)["reached"])

    def has_actionable_step(self, observation: UIObservation) -> bool:
        """Whether the AI sees a concrete login/onboarding control to act on now.

        Consulted by the agent's wait-gate: when the model reports the screen is a
        transient loading/splash state with no real sign-in/onboarding control
        (even if it carries an incidental link), this returns False so the agent
        WAITS and re-observes instead of asking the Brain - which is exactly what
        stops the model from pressing a diagnostic Back on a 'looking for
        accounts' screen. Foreground stays a deterministic precondition."""
        if self._foreground_check is not None and not self._foreground_check():
            return False
        verdict = self._evaluate("", observation)
        return bool(verdict.get("actionable_step")) or bool(verdict.get("reached"))

    def _evaluate(self, goal: str, observation: UIObservation) -> dict:
        key = self._fingerprint(observation)
        if self._cache_key == key and self._cache_val is not None:
            return self._cache_val
        verdict = self._request(goal, observation)
        self._cache_key = key
        self._cache_val = verdict
        return verdict

    def _request(self, goal: str, observation: UIObservation) -> dict:
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": self._render(goal, observation)},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._transport(payload)
            content = response["choices"][0]["message"]["content"]
            decoded = json.loads(content)
        except (KeyError, IndexError, ValueError, TypeError, RuntimeError):
            # Fail safe: never falsely report login done (reached=False). Leave
            # actionable_step=True so a transient judge/transport failure does not
            # trap a real sign-in screen in the wait loop - the Brain (a separate
            # model) can still be asked to drive login, as it was before.
            return {"reached": False, "actionable_step": True}
        reached = decoded.get("reached")
        actionable_step = decoded.get("actionable_step")
        if type(reached) is not bool or type(actionable_step) is not bool:
            return {"reached": False, "actionable_step": False}
        return {"reached": reached, "actionable_step": actionable_step}

    @staticmethod
    def _fingerprint(observation: UIObservation) -> tuple:
        # Stable, secret-free screen signature (credential values are already
        # redacted by the observer). Identical screens reuse one verdict.
        return tuple(
            (element.resource_id, element.label, element.clickable, element.is_input)
            for element in observation.elements
        )

    @staticmethod
    def _render(goal: str, observation: UIObservation) -> str:
        # observation.describe() already redacts credential fields, so no secret
        # can reach the judge prompt.
        return (
            f"GOAL: {goal}\n"
            "CURRENT UI ELEMENTS:\n"
            f"{observation.describe(limit=40)}\n"
            "Respond with JSON: "
            '{"reached": true|false, "actionable_step": true|false, '
            '"reason": "..."}'
        )

    def _http_transport(self, payload: dict) -> dict:
        try:
            return self._client.send(payload)
        except ModelTransportError as error:
            raise RuntimeError(f"Login judge request failed: {error}") from error


class AuthoritativeLoginGoalEvaluator:
    """Production login boundary: deterministic evidence first, AI if ambiguous."""

    def __init__(
        self,
        deterministic: SignedInCopilotGoalEvaluator,
        semantic: "LLMLoginGoalEvaluator | None",
    ) -> None:
        self._deterministic = deterministic
        self._semantic = semantic

    def begin_run(self) -> None:
        if self._semantic is not None:
            self._semantic.begin_run()

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        verdict = self._deterministic.deterministic_verdict(observation)
        if verdict is not None:
            return verdict
        if self._semantic is not None:
            return self._semantic.is_reached(goal, observation)
        return self._deterministic.is_reached(goal, observation)

    def has_actionable_step(self, observation: UIObservation) -> bool:
        verdict = self._deterministic.deterministic_actionable_verdict(observation)
        if verdict is not None:
            return verdict
        if self._semantic is not None:
            return self._semantic.has_actionable_step(observation)
        return self._deterministic.has_actionable_step(observation)

    def failure_reason(self, observation: UIObservation) -> "str | None":
        return self._deterministic.failure_reason(observation)



class SemanticStateEvaluator:
    """Generic model-backed judge: is a natural-language screen state displayed?

    Reusable by any use case via SemanticTerminalState. It knows nothing about
    login, Deeplink, or FRI - the caller's natural-language ``description`` carries
    all the intent. Fail-safe: any transport/decode error yields "not matched" so
    a transient failure never fabricates a terminal state.
    """

    _SYSTEM_PROMPT = (
        "You judge whether a described Android screen state is CURRENTLY on "
        "screen, using ONLY the provided UI elements. Do not guess about screens "
        "you cannot see. Respond strictly as JSON."
    )

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: "Callable[[dict], dict] | None" = None,
        timeout: float = 60.0,
    ) -> None:
        self._client = ChatModelClient(
            model=model, api_key=api_key, base_url=base_url, timeout=timeout
        )
        self._model = model
        self._transport = transport or self._http_transport
        self._cache_key: tuple | None = None
        self._cache_val: dict | None = None

    def begin_run(self) -> None:
        self._cache_key = None
        self._cache_val = None

    @classmethod
    def from_env(
        cls,
        env: dict | None = None,
        *,
        transport: "Callable[[dict], dict] | None" = None,
    ) -> "SemanticStateEvaluator | None":
        config = ChatModelClient.config_from_env(env)
        if config is None:
            return None
        return cls(
            model=config["model"],
            api_key=config["api_key"],
            base_url=config["base_url"],
            transport=transport,
        )

    def matches(self, description: str, observation: UIObservation) -> bool:
        return bool(self._evaluate(description, observation).get("matches"))

    def _evaluate(self, description: str, observation: UIObservation) -> dict:
        key = (description, self._fingerprint(observation))
        if self._cache_key == key and self._cache_val is not None:
            return self._cache_val
        verdict = self._request(description, observation)
        self._cache_key = key
        self._cache_val = verdict
        return verdict

    def _request(self, description: str, observation: UIObservation) -> dict:
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": self._render(description, observation)},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._transport(payload)
            content = response["choices"][0]["message"]["content"]
            decoded = json.loads(content)
        except (KeyError, IndexError, ValueError, TypeError, RuntimeError):
            return {"matches": False}
        matches = decoded.get("matches")
        if type(matches) is not bool:
            return {"matches": False}
        return {"matches": matches}

    @staticmethod
    def _fingerprint(observation: UIObservation) -> tuple:
        return tuple(
            (element.resource_id, element.label, element.clickable, element.is_input)
            for element in observation.elements
        )

    @staticmethod
    def _render(description: str, observation: UIObservation) -> str:
        return (
            f"SCREEN STATE TO CHECK: {description}\n"
            "CURRENT UI ELEMENTS:\n"
            f"{observation.describe(limit=40)}\n"
            'Respond with JSON: {"matches": true|false, "reason": "..."}'
        )

    def _http_transport(self, payload: dict) -> dict:
        try:
            return self._client.send(payload)
        except ModelTransportError as error:
            raise RuntimeError(f"Semantic state judge request failed: {error}") from error
