"""AppPilot login/onboarding flow.

The generic AppPilotAgent + brain decide each action; this module only supplies
the login goal, guidance, deterministic goal evaluators, and the CLI entry
point. No UI action sequence is hardcoded here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Callable

try:  # package-relative (python -m src.flows.login / -m src.apppilot_agent)
    from ..apppilot.agent import (
    AppPilotAgent,
    DEFAULT_MAX_ACTIONS,
    DEFAULT_MAX_STUCK_ACTIONS,
    _default_max_actions,
    _default_max_stuck_actions,
    _load_dotenv,
    )
    from ..apppilot.android import APP_ID, MaestroExecutor, MaestroHierarchyObserver
    from ..apppilot.brain import (
        LLMModelDecisionProvider,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from ..apppilot.models import RuntimeContext, UIElement, UIObservation
    from ..apppilot.safety import SafetyValidator, infer_credential_kind
    from ..apppilot import logtags
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.agent import (
    AppPilotAgent,
    DEFAULT_MAX_ACTIONS,
    DEFAULT_MAX_STUCK_ACTIONS,
    _default_max_actions,
    _default_max_stuck_actions,
    _load_dotenv,
    )
    from apppilot.android import APP_ID, MaestroExecutor, MaestroHierarchyObserver
    from apppilot.brain import (
        LLMModelDecisionProvider,
        ModelDecisionProvider,
        UnconfiguredModelDecisionProvider,
    )
    from apppilot.models import RuntimeContext, UIElement, UIObservation
    from apppilot.safety import SafetyValidator, infer_credential_kind
    from apppilot import logtags


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
    "the actual value securely, so never type a credential yourself. Dismiss "
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
        # Login is not complete unless the TARGET app is actually foreground -
        # available credentials or a prior signed-in state never mean "complete"
        # while another window (e.g. the launching store window) is in front.
        if self._foreground_check is not None and not self._foreground_check():
            return False
        return not self._blocked(observation) and self._settled_inside(observation)

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
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._foreground_check = foreground_check
        self._transport = transport or self._http_transport
        self._timeout = timeout
        # 1-entry cache so is_reached() and has_actionable_step(), which the agent
        # calls on the SAME observation each step, share ONE model call. Keyed by
        # a stable, secret-free fingerprint of the screen.
        self._cache_key: tuple | None = None
        self._cache_val: dict | None = None

    @classmethod
    def from_env(
        cls,
        env: dict | None = None,
        *,
        foreground_check: "Callable[[], bool] | None" = None,
        transport: "Callable[[dict], dict] | None" = None,
    ) -> "LLMLoginGoalEvaluator | None":
        env = os.environ if env is None else env
        api_key = env.get("APPPILOT_MODEL_API_KEY")
        model = env.get("APPPILOT_MODEL")
        if not api_key or not model:
            return None
        base_url = env.get("APPPILOT_MODEL_BASE_URL") or "https://api.openai.com/v1"
        return cls(
            model=model,
            api_key=api_key,
            base_url=base_url,
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
        return {
            "reached": bool(decoded.get("reached")),
            "actionable_step": bool(decoded.get("actionable_step")),
        }

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
            raise RuntimeError(f"Login judge request failed: {error}") from error


def resolve_decision_provider(
    provider: ModelDecisionProvider | None = None,
) -> ModelDecisionProvider:
    """Return the given provider, else the env-configured LLM provider, else the
    honest Unconfigured placeholder. Centralized so the CLI and other flows
    (e.g. the deeplink runner) share one provider-resolution policy."""
    if provider is not None:
        return provider
    provider = LLMModelDecisionProvider.from_env()
    if provider is None:
        provider = UnconfiguredModelDecisionProvider(
            "Set APPPILOT_MODEL and APPPILOT_MODEL_API_KEY (and optionally "
            "APPPILOT_MODEL_BASE_URL) to connect a real decision model."
        )
    return provider


def build_login_agent(
    device: str = "emulator-5554",
    *,
    provider: ModelDecisionProvider | None = None,
    observer: MaestroHierarchyObserver | None = None,
    executor: MaestroExecutor | None = None,
    max_actions: int | None = None,
    max_stuck_actions: int | None = None,
    foreground_check: "Callable[[], bool] | None" = None,
) -> AppPilotAgent:
    """Build the single, shared login/onboarding agent (AppPilotAgent + Brain).

    No UI steps hardcoded: the model decides each action. The STOP evaluator is
    the AI-backed LLMLoginGoalEvaluator when a model is configured, else the
    deterministic marker-based SignedInCopilotGoalEvaluator (offline fallback).
    Both honor ``foreground_check`` so login never completes while the app is not
    foreground. Callers may pass an existing observer/executor to reuse the same
    Android infrastructure (DRY).
    """
    goal_evaluator = LLMLoginGoalEvaluator.from_env(
        foreground_check=foreground_check
    ) or SignedInCopilotGoalEvaluator(foreground_check=foreground_check)
    # Let the login evaluator classify whether the screen presents a genuine
    # login/onboarding step, so the agent waits/re-observes on transient loading
    # screens (e.g. "looking for accounts" with only a "Terms of use" link)
    # instead of asking the Brain, which would pick a diagnostic Back.
    actionable_step_check = getattr(goal_evaluator, "has_actionable_step", None)
    return AppPilotAgent(
        observer=observer or MaestroHierarchyObserver(device),
        goal_evaluator=goal_evaluator,
        decision_provider=resolve_decision_provider(provider),
        safety_validator=SafetyValidator(),
        executor=executor or MaestroExecutor(APP_ID, device),
        max_actions=max_actions if max_actions is not None else _default_max_actions(),
        runtime_context=RuntimeContext.from_env(),
        max_stuck_actions=(
            max_stuck_actions
            if max_stuck_actions is not None
            else _default_max_stuck_actions()
        ),
        actionable_step_check=actionable_step_check,
        log_tag=logtags.LOGIN,
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

    agent = build_login_agent(
        args.device,
        max_actions=args.max_actions,
        max_stuck_actions=args.max_stuck_actions,
    )
    try:
        return 0 if agent.run(PROTOTYPE_GOAL, args.guidance) else 1
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"{logtags.tag(logtags.LOGIN)} RESULT:\nFAIL - {error}")
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
