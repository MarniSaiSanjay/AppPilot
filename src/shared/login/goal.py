"""Shared login goal evaluators."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable

try:
    from ...apppilot import logtags
    from ...apppilot.models import UIElement, UIObservation
    from ...apppilot.safety import infer_credential_kind
    from ..model_client import ChatModelClient, ModelTransportError
except ImportError:
    from apppilot import logtags
    from apppilot.models import UIElement, UIObservation
    from apppilot.safety import infer_credential_kind
    from shared.model_client import ChatModelClient, ModelTransportError


_LOGIN_GOAL_CACHE_VERSION = 2
_DEFAULT_MAX_LOGIN_GOAL_CACHE_ENTRIES = 200
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


PROTOTYPE_GOAL = (
    "Complete authentication and all required first-launch or post-authentication "
    "onboarding. Dismiss every blocking dialog, bottom sheet, popup, privacy "
    "notice, notification prompt, and suggested-prompt/welcome interruption. "
    "Login is complete only after an unobstructed normal destination such as "
    "Chat, Search, or Researcher remains visible and usable. Then stop and return "
    "control to the caller. Do NOT navigate further into Copilot, do NOT tap "
    "suggested actions or 'Ask Copilot', and do NOT open files or send prompts "
    "to prove Copilot is usable."
)
DEFAULT_GUIDANCE = (
    "Advance ONLY through sign-in and required first-launch or post-authentication "
    "onboarding. Treat every blocking dialog, bottom sheet, popup, privacy notice, "
    "notification prompt, and welcome interruption as part of login; acknowledge "
    "or dismiss it with the safest non-destructive control and keep going until "
    "the normal Chat, Search, or Researcher destination is visible without an "
    "overlay. When a "
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
    "close it with the X instead of sending the suggestion. Do NOT continue into "
    "the app afterwards: once the unobstructed destination is stable, take no "
    "further actions - that screen belongs to the caller, not to login."
)



class SignedInCopilotGoalEvaluator:
    """Detects when shared login has prepared the app."""

    def __init__(
        self, foreground_check: "Callable[[], bool] | None" = None
    ) -> None:
        self._foreground_check = foreground_check

    _HOME_TEXT = ("message copilot",)
    _HOME_RESOURCE = ("home_screen", "copilot_composer", "copilot_input")
    _COMPOSER_INPUT_TEXT = (
        "message copilot",
        "ask copilot",
        "message m365 copilot",
        "message microsoft 365 copilot",
    )
    _COMPOSER_INPUT_RESOURCE = ("copilot", "composer")
    _SEARCH_INPUT_TEXT = ("search",)
    _SEARCH_INPUT_RESOURCE = ("search_box", "search_input", "search_field")
    _CURRENT_ACCOUNT_RESOURCE = ("current-account-custom-link",)
    _SIGN_OUT_RESOURCE = ("sign-out-item-button",)
    _AUTH_WEBVIEW_RESOURCE = (
        "oneauth_navigation_web_view",
        "common_auth_webview",
    )
    _AUTH_LOADING_TEXT = ("looking for accounts", "reviewing accounts")
    _SAFE_PERMISSION_DISMISS_RESOURCE = ("permission_deny",)
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
        "couldn't find a microsoft account",
        "could not find a microsoft account",
        "couldn't find an account with that email address or phone number",
        "could not find an account with that email address or phone number",
        "too many times with an incorrect account or password",
    )
    _NEGATIVE_AUTH_RESOURCE = (
        "access_denied",
        "access_restricted",
        "account_blocked",
        "signin_error",
    )
    _INTRO = (
        "let's get started",
        "lets get started",
        "suggested prompt",
        "suggested_prompt",
        "suggestion_card",
        "prompt_starter",
        "intro_suggestion",
    )
    _REQUIRED_ONBOARDING = (
        "microsoft respects your privacy",
        "your privacy option",
        "your privacy matters",
        "diagnostic data for microsoft 365",
        "privacy settings applied",
        "meet the latest copilot",
        "getting better together",
        "powering your experiences",
        "don't miss anything",
        "don’t miss anything",
    )
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
        return self._settled_inside(observation)

    def deterministic_verdict(
        self, observation: UIObservation
    ) -> "bool | None":
        """Return a definite login verdict, or None for an ambiguous app screen."""
        if self._foreground_check is not None and not self._foreground_check():
            return False
        if self._auth_webview_shell(observation):
            return False
        if self._matches(observation, text=self._AUTH_LOADING_TEXT):
            return False
        if self._blocked(observation):
            return False
        if self._negative_auth_terminal(observation):
            return False
        if self._signed_in_home(observation) or self._search_screen(observation):
            return None
        if not self._has_actionable_ui(observation):
            return False
        return None

    def deterministic_actionable_verdict(
        self, observation: UIObservation
    ) -> "bool | None":
        """Return whether a definite login step exists, or None if ambiguous."""
        if self._matches(
            observation,
            resource=self._SAFE_PERMISSION_DISMISS_RESOURCE,
        ):
            return True
        if self._foreground_check is not None and not self._foreground_check():
            return False
        if self._auth_webview_shell(observation):
            return False
        if self._matches(observation, text=self._AUTH_LOADING_TEXT):
            return False
        if self._blocked(observation):
            return True
        if self._negative_auth_terminal(observation):
            return False
        if self._signed_in_home(observation) or self._search_screen(observation):
            return None
        if not self._has_actionable_ui(observation):
            return False
        return None

    def _blocked(self, observation: UIObservation) -> bool:
        return (
            self._authenticating(observation)
            or self._intro_present(observation)
            or self._matches(observation, text=self._REQUIRED_ONBOARDING)
            or self._signed_in_account_sheet(observation)
        )

    def has_actionable_step(self, observation: UIObservation) -> bool:
        """Whether this screen has a login/onboarding step to act on."""
        if self._matches(
            observation,
            resource=self._SAFE_PERMISSION_DISMISS_RESOURCE,
        ):
            return True
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

    def _signed_in_account_sheet(self, observation: UIObservation) -> bool:
        return self._matches(
            observation,
            resource=self._CURRENT_ACCOUNT_RESOURCE,
        ) and self._matches(
            observation,
            resource=self._SIGN_OUT_RESOURCE,
        )

    def _auth_webview_shell(self, observation: UIObservation) -> bool:
        actionable = [
            element
            for element in observation.elements
            if element.clickable or element.is_input
        ]
        return bool(actionable) and all(
            any(
                marker in element.resource_id.casefold()
                for marker in self._AUTH_WEBVIEW_RESOURCE
            )
            for element in actionable
        )

    def _negative_auth_terminal(self, observation: UIObservation) -> bool:
        return self._matches(
            observation,
            text=self._NEGATIVE_AUTH_TEXT,
            resource=self._NEGATIVE_AUTH_RESOURCE,
        )

    @staticmethod
    def _has_actionable_ui(observation: UIObservation) -> bool:
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
        for element in observation.elements:
            if not element.is_input:
                continue
            if infer_credential_kind(
                element.resource_id, element.hint_text, element.class_name
            ) is not None:
                return True
        return False


class LLMLoginGoalEvaluator:
    """Model-backed login-completion judge."""

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
        "welcome / onboarding interruption that should be dismissed first, any "
        "blocking post-authentication dialog, bottom sheet, popup, privacy "
        "notice, or notification prompt, OR a "
        "transient loading / splash / 'looking for account' screen that has not "
        "settled yet.\n"
        "   - reached=true ONLY when the screen is clearly a normal, usable "
        "IN-APP screen that belongs to the signed-in user (for example a home / "
        "chat / composer / search / content screen) with no sign-in affordance "
        "and no pending onboarding or post-authentication interruption.\n"
        "\n"
        "2) actionable_step: is there a CONCRETE sign-in / authentication / "
        "onboarding control that the automation should act on RIGHT NOW - for "
        "example a sign-in or 'Continue with <provider>' button, an account to "
        "pick, a visible email/password field, or an intro/suggested-prompt to "
        "dismiss, including a safe acknowledgement/close action on a blocking "
        "post-authentication dialog, bottom sheet, popup, privacy notice, or "
        "notification prompt?\n"
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
        base_url: str,
        *,
        foreground_check: "Callable[[], bool] | None" = None,
        transport: "Callable[[dict], dict] | None" = None,
        timeout: float = 60.0,
        cache_path: "Path | None" = None,
        max_cache_entries: int = _DEFAULT_MAX_LOGIN_GOAL_CACHE_ENTRIES,
    ) -> None:
        self._client = ChatModelClient(
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        self._model = model
        self._foreground_check = foreground_check
        self._transport = transport or self._http_transport
        self._cache_path = cache_path or self._default_cache_path()
        self._max_cache_entries = max(1, max_cache_entries)
        self._persistent_cache = self._load_cache()
        self._pending_cache: dict[str, dict[str, bool]] = {}
        self._cache_key: tuple | None = None
        self._cache_val: dict | None = None

    def begin_run(self) -> None:
        self._cache_key = None
        self._cache_val = None
        self._pending_cache.clear()

    def finish_run(self, succeeded: bool) -> None:
        if succeeded and self._pending_cache:
            self._persistent_cache.update(self._pending_cache)
            while len(self._persistent_cache) > self._max_cache_entries:
                self._persistent_cache.pop(next(iter(self._persistent_cache)))
            self._save_cache()
        self._pending_cache.clear()
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
            base_url=config["base_url"],
            foreground_check=foreground_check,
            transport=transport,
        )

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        if self._foreground_check is not None and not self._foreground_check():
            return False
        return bool(self._evaluate(goal, observation)["reached"])

    def has_actionable_step(self, observation: UIObservation) -> bool:
        """Whether the model sees a login/onboarding control to act on."""
        if self._foreground_check is not None and not self._foreground_check():
            return False
        verdict = self._evaluate("", observation)
        return bool(verdict.get("actionable_step")) or bool(verdict.get("reached"))

    def _evaluate(self, goal: str, observation: UIObservation) -> dict:
        fingerprint = self._fingerprint(observation)
        key = (goal, fingerprint)
        if self._cache_key == key and self._cache_val is not None:
            return self._cache_val
        persistent_key = self._persistent_key(goal, fingerprint)
        persisted = self._pending_cache.get(
            persistent_key,
            self._persistent_cache.get(persistent_key),
        )
        if persisted is not None:
            self._cache_key = key
            self._cache_val = dict(persisted)
            return self._cache_val
        verdict = self._request(goal, observation)
        cacheable = verdict.pop("_cacheable", False) is True
        self._cache_key = key
        self._cache_val = verdict
        if cacheable:
            self._pending_cache[persistent_key] = dict(verdict)
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
            if not isinstance(decoded, dict):
                raise TypeError("model response content must be a JSON object")
        except (KeyError, IndexError, ValueError, TypeError, RuntimeError):
            return {
                "reached": False,
                "actionable_step": True,
                "_cacheable": False,
            }
        reached = decoded.get("reached")
        actionable_step = decoded.get("actionable_step")
        if type(reached) is not bool or type(actionable_step) is not bool:
            return {
                "reached": False,
                "actionable_step": False,
                "_cacheable": False,
            }
        return {
            "reached": reached,
            "actionable_step": actionable_step,
            "_cacheable": True,
        }

    @staticmethod
    def _fingerprint(observation: UIObservation) -> tuple:
        return tuple(
            (
                element.element_id,
                element.parent_id,
                element.resource_id,
                element.label,
                element.clickable,
                element.enabled,
                element.is_input,
            )
            for element in observation.elements
        )

    def _persistent_key(self, goal: str, fingerprint: tuple) -> str:
        raw = json.dumps(
            {
                "version": _LOGIN_GOAL_CACHE_VERSION,
                "model": self._model,
                "goal": goal,
                "fingerprint": fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _default_cache_path() -> Path:
        root = Path(
            os.environ.get(
                "XDG_CACHE_HOME",
                Path.home() / ".cache",
            )
        )
        return (
            root
            / "apppilot"
            / f"login-goal-verdicts-v{_LOGIN_GOAL_CACHE_VERSION}.json"
        )

    def _load_cache(self) -> dict[str, dict[str, bool]]:
        if not self._cache_path.exists():
            return {}
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            entries = payload.get("entries", {})
            if (
                payload.get("version") != _LOGIN_GOAL_CACHE_VERSION
                or not isinstance(entries, dict)
            ):
                raise ValueError("unsupported cache format")
            return {
                key: {
                    "reached": value["reached"],
                    "actionable_step": value["actionable_step"],
                }
                for key, value in entries.items()
                if isinstance(key, str)
                and _SHA256.fullmatch(key)
                and isinstance(value, dict)
                and type(value.get("reached")) is bool
                and type(value.get("actionable_step")) is bool
            }
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            logtags.trace(
                f"login goal cache ignored: {error}",
                logtags.LOGIN,
            )
            return {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": _LOGIN_GOAL_CACHE_VERSION,
                        "entries": self._persistent_cache,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._cache_path)
        except (OSError, UnicodeError) as error:
            logtags.trace(
                f"login goal cache was not saved: {error}",
                logtags.LOGIN,
            )

    @staticmethod
    def _render(goal: str, observation: UIObservation) -> str:
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
    """Production login boundary evaluator."""

    _REQUIRED_COMPLETION_OBSERVATIONS = 3

    def __init__(
        self,
        deterministic: SignedInCopilotGoalEvaluator,
        semantic: "LLMLoginGoalEvaluator | None",
    ) -> None:
        self._deterministic = deterministic
        self._semantic = semantic
        self._completion_observations = 0
        self._confirming_current_observation = False

    def begin_run(self) -> None:
        self._completion_observations = 0
        self._confirming_current_observation = False
        if self._semantic is not None:
            self._semantic.begin_run()

    def finish_run(self, succeeded: bool) -> None:
        if self._semantic is not None:
            self._semantic.finish_run(succeeded)

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        self._confirming_current_observation = False
        verdict = self._deterministic.deterministic_verdict(observation)
        if verdict is None:
            if self._semantic is not None:
                verdict = self._semantic.is_reached(goal, observation)
            else:
                verdict = self._deterministic.is_reached(goal, observation)
        if not verdict:
            self._completion_observations = 0
            return False

        self._completion_observations += 1
        if (
            self._completion_observations
            < self._REQUIRED_COMPLETION_OBSERVATIONS
        ):
            self._confirming_current_observation = True
            return False

        self._completion_observations = 0
        return True

    def has_actionable_step(self, observation: UIObservation) -> bool:
        if self._confirming_current_observation:
            return False
        verdict = self._deterministic.deterministic_actionable_verdict(observation)
        if verdict is not None:
            return verdict
        if self._semantic is not None:
            return self._semantic.has_actionable_step(observation)
        return self._deterministic.has_actionable_step(observation)

    def failure_reason(self, observation: UIObservation) -> "str | None":
        return self._deterministic.failure_reason(observation)



class SemanticStateEvaluator:
    """Generic model-backed screen-state judge."""

    _SYSTEM_PROMPT = (
        "You judge whether a described Android screen state is CURRENTLY on "
        "screen, using ONLY the provided UI elements. Do not guess about screens "
        "you cannot see. Respond strictly as JSON."
    )

    def __init__(
        self,
        model: str,
        base_url: str,
        *,
        transport: "Callable[[dict], dict] | None" = None,
        timeout: float = 60.0,
    ) -> None:
        self._client = ChatModelClient(
            model=model,
            base_url=base_url,
            timeout=timeout,
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
            if not isinstance(decoded, dict):
                raise TypeError("model response content must be a JSON object")
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
