"""Generic AppPilot agent orchestration (observe -> decide -> validate -> act)."""

from __future__ import annotations

import os
from pathlib import Path

from .android import MaestroExecutor, MaestroHierarchyObserver
from .brain import DecisionRequest, ModelDecisionProvider
from .models import ExecutionContext, GoalEvaluator, RuntimeContext
from .safety import SafetyValidator

REPO_ROOT = Path(__file__).resolve().parents[2]
# Safety bound on the number of actions per run, to prevent an infinite agent
# loop. Configurable via --max-actions or the APPPILOT_MAX_ACTIONS env var; this
# is only the fallback default.
DEFAULT_MAX_ACTIONS = 30
# Stop with a controlled FAIL after this many actions without a meaningful UI
# change. Configurable via --max-stuck-actions or APPPILOT_MAX_STUCK_ACTIONS.
DEFAULT_MAX_STUCK_ACTIONS = 5


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
