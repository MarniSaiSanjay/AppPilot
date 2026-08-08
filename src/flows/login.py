"""AppPilot login/onboarding flow.

The generic AppPilotAgent + brain decide each action; this module only supplies
the login goal, guidance, deterministic goal evaluators, and the CLI entry
point. No UI action sequence is hardcoded here.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

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
