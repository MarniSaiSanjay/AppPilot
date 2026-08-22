"""Generic AppPilot agent orchestration (observe -> decide -> validate -> act)."""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .android import MaestroExecutor, MaestroHierarchyObserver
from .brain import DecisionRequest, ModelDecisionProvider
from . import logtags
from .models import (
    Action,
    ActionKind,
    ExecutionContext,
    GoalEvaluator,
    RuntimeContext,
    UIElement,
    UIObservation,
)
from .safety import SafetyValidator

REPO_ROOT = Path(__file__).resolve().parents[2]
# Safety bound on the number of actions per run, to prevent an infinite agent
# loop. Configurable via --max-actions or the APPPILOT_MAX_ACTIONS env var; this
# is only the fallback default.
DEFAULT_MAX_ACTIONS = 30
# Stop with a controlled FAIL after this many actions without a meaningful UI
# change. Configurable via --max-stuck-actions or APPPILOT_MAX_STUCK_ACTIONS.
DEFAULT_MAX_STUCK_ACTIONS = 5
# Generic loading/transition handling: when the goal is not reached AND the
# screen exposes no actionable UI (a transient loading/sync state), the agent
# waits this long and re-observes instead of asking the Brain to invent an
# action. Bounded by DEFAULT_MAX_NONACTIONABLE_WAITS so it can never loop
# forever. These are deliberately generic (no string/coordinate detection).
DEFAULT_NONACTIONABLE_WAIT_SECONDS = 2.0
DEFAULT_MAX_NONACTIONABLE_WAITS = 10


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
        sleep: "Callable[[float], None]" = time.sleep,
        nonactionable_wait_seconds: float = DEFAULT_NONACTIONABLE_WAIT_SECONDS,
        max_nonactionable_waits: int = DEFAULT_MAX_NONACTIONABLE_WAITS,
        actionable_step_check: "Callable[[object], bool] | None" = None,
        log_tag: str = "",
    ) -> None:
        self._observer = observer
        self._goal_evaluator = goal_evaluator
        self._decision_provider = decision_provider
        self._safety_validator = safety_validator
        self._executor = executor
        self._max_actions = max_actions
        self._runtime_context = runtime_context
        self._max_stuck_actions = max_stuck_actions
        self._sleep = sleep
        self._nonactionable_wait_seconds = nonactionable_wait_seconds
        self._max_nonactionable_waits = max_nonactionable_waits
        # Optional domain gate: returns True only when the observation presents a
        # genuine actionable step for THIS flow, so an incidental control (e.g. a
        # "Terms of use" link on a loading splash) is not mistaken for one. When
        # None, any non-Back action counts as actionable.
        self._actionable_step_check = actionable_step_check
        # Optional subsystem tag (e.g. "LOGIN") prefixed to every emitted line so
        # the trace is greppable and this run's PASS is never read as a whole test
        # case passing. Empty => untagged lines (generic reuse).
        self._log_tag = log_tag

    def _emit(self, text: str) -> None:
        """Print one log entry, prefixed with the subsystem tag when set."""
        print(logtags.prefix(self._log_tag, text))

    def _log_goal_reached(self, reached: bool) -> None:
        self._emit(f"GOAL REACHED:\n{str(reached).lower()}\n")

    def _log_pass(self) -> None:
        self._emit("RESULT:\nPASS")

    def _log_fail(self, detail: str) -> None:
        self._emit(f"RESULT:\nFAIL - {detail}")

    def run(self, goal: str, guidance: str | None = None) -> bool:
        reset_recovery = getattr(self._observer, "reset_recovery_budget", None)
        if callable(reset_recovery):
            reset_recovery()
        self._emit(f"GOAL:\n{goal}\n")
        if guidance:
            self._emit(f"GUIDANCE:\n{guidance}\n")
        self._emit(f"MAX ACTIONS:\n{self._max_actions}\n")
        self._emit(f"MAX STUCK ACTIONS:\n{self._max_stuck_actions}\n")

        history: list[str] = []
        # Track the last credential we entered and the screen it was entered on,
        # to avoid re-entering the same credential when the UI has not changed.
        last_credential_key: tuple[str, str] | None = None
        last_credential_fingerprint: tuple | None = None
        # Credentials already entered on the current (unchanged) screen. Their
        # input actions are withheld from the model until the screen changes, so
        # it advances to submit instead of re-typing a field that looks empty (a
        # password box never echoes its value, so the screen appears unchanged).
        filled_credential_keys: set[tuple[str, str]] = set()
        filled_fingerprint: tuple | None = None
        # Track consecutive actions that leave the meaningful UI unchanged.
        last_acted_fingerprint: tuple | None = None
        consecutive_stuck = 0
        for step in range(self._max_actions + 1):
            observation = self._observer.observe()
            self._emit(f"OBSERVE:\n{observation.describe()}\n")

            reached = self._goal_evaluator.is_reached(goal, observation)
            self._log_goal_reached(reached)
            if reached:
                self._log_pass()
                return True
            failure_reason = self._terminal_failure_reason(observation)
            if failure_reason is not None:
                self._log_fail(failure_reason)
                return False

            # Loading/transition invariant: when the goal is not reached and no
            # actionable step exists, wait and re-observe (never invent an action
            # or a diagnostic Back) until a step appears, the goal is reached, or
            # the wait budget is exhausted.
            available_actions, filled_fingerprint = self._offer_actions(
                observation, filled_credential_keys, filled_fingerprint
            )
            waits = 0
            while not self._has_actionable_step(observation, available_actions):
                if waits >= self._max_nonactionable_waits:
                    self._log_fail(
                        f"no actionable step appeared after {waits} wait(s); app "
                        "stayed in a loading/transition state with no "
                        "login/onboarding action to take"
                    )
                    return False
                waits += 1
                self._emit(
                    "WAIT:\nno actionable step; re-observing "
                    f"({waits}/{self._max_nonactionable_waits})\n"
                )
                self._sleep(self._nonactionable_wait_seconds)
                observation = self._observer.observe()
                self._emit(f"OBSERVE:\n{observation.describe()}\n")
                reached = self._goal_evaluator.is_reached(goal, observation)
                self._log_goal_reached(reached)
                if reached:
                    self._log_pass()
                    return True
                failure_reason = self._terminal_failure_reason(observation)
                if failure_reason is not None:
                    self._log_fail(failure_reason)
                    return False
                available_actions, filled_fingerprint = self._offer_actions(
                    observation, filled_credential_keys, filled_fingerprint
                )

            # Advance the stuck counter when the last action left the meaningful
            # UI unchanged; a meaningful change resets it. Only counts once an
            # action has been taken (last_acted_fingerprint is set).
            meaningful_fingerprint = self._meaningful_fingerprint(observation)
            if last_acted_fingerprint is not None:
                if meaningful_fingerprint == last_acted_fingerprint:
                    consecutive_stuck += 1
                else:
                    consecutive_stuck = 0
            self._emit(f"PROGRESS:\nstuck {consecutive_stuck}/{self._max_stuck_actions}\n")
            if consecutive_stuck >= self._max_stuck_actions:
                self._log_fail(
                    "agent appears stuck: no meaningful UI change for "
                    f"{consecutive_stuck} consecutive actions"
                )
                return False

            if step == self._max_actions:
                self._log_fail(f"action/step limit reached ({self._max_actions})")
                return False

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
                self._emit(
                    "MODEL DECISION:\ncannot safely proceed\n"
                    f"Reason: {decision.reason}\n"
                )
                self._log_fail("model cannot safely proceed")
                return False

            self._emit(
                f"MODEL DECISION:\n{decision.action.describe(observation)}\n"
                f"Reason: {decision.reason}\n"
            )

            fresh_observation = self._reobserve_before_action()
            self._emit(f"RE-OBSERVE:\n{fresh_observation.describe()}\n")
            current_action = self._rebind_current_action(
                decision.action, observation, fresh_observation
            )
            if current_action is None:
                self._emit(
                    "STALE DECISION:\ndiscarded because the UI changed before "
                    "execution\n"
                )
                continue
            observation = fresh_observation
            meaningful_fingerprint = self._meaningful_fingerprint(observation)

            try:
                self._safety_validator.validate(current_action, observation)
            except ValueError as error:
                self._emit(f"SAFETY VALIDATION:\nrejected - {error}\n")
                self._log_fail("model proposed an unsafe or invalid action")
                return False
            self._emit("SAFETY VALIDATION:\npassed\n")

            # Resolve any requested credential locally, after safety validation.
            # The secret value is never printed and never leaves this scope
            # except to be handed directly to Maestro.
            secret: str | None = None
            action = current_action
            if action.credential_kind is not None:
                if not self._runtime_context.has(action.credential_kind):
                    self._emit(
                        "CREDENTIAL:\nrequired "
                        f"{action.credential_kind.value} is not configured\n"
                    )
                    self._log_fail(
                        "required credential is not configured "
                        f"({action.credential_kind.value})"
                    )
                    return False

                # Guard against re-entering the same credential on an unchanged
                # screen (prevents redundant entry loops). The model remains the
                # decision-maker; this is only a safety guard, not a login step.
                credential_key = self._credential_field_key(action, observation)
                fingerprint = self._observation_fingerprint(observation)
                if (
                    credential_key == last_credential_key
                    and fingerprint == last_credential_fingerprint
                ):
                    self._emit(
                        "CREDENTIAL:\n"
                        f"{action.credential_kind.value} already entered on this "
                        "screen; UI unchanged\n"
                    )
                    self._log_fail(
                        "repeated credential entry with no UI change (possible loop)"
                    )
                    return False

                secret = self._runtime_context.resolve(action.credential_kind)
                last_credential_key = credential_key
                last_credential_fingerprint = fingerprint

            self._emit(f"ACTION:\n{action.describe(observation)}\n")
            self._executor.execute(action, observation, secret=secret)
            if action.credential_kind is not None:
                # Withhold this field's input next turn (until the meaningful UI
                # changes) so the model proceeds to submit rather than re-typing
                # it. Set the withhold baseline from the observation we actually
                # acted on (fresh_observation, post-reobserve/rebind), not the
                # pre-reobserve one, so next turn compares against the right
                # screen. ``meaningful_fingerprint`` is that observation's.
                filled_credential_keys.add(
                    self._credential_field_key(action, observation)
                )
                filled_fingerprint = meaningful_fingerprint
            history.append(action.describe(observation))
            # Remember the state we just acted on, to detect progress next step.
            last_acted_fingerprint = meaningful_fingerprint

        raise AssertionError("Agent loop exited unexpectedly")

    def _has_actionable_step(self, observation, available_actions) -> bool:
        """Whether the agent should act now, or wait/re-observe instead.

        Two conditions must hold:

        1. The observation exposes at least one non-Back action (otherwise the
           screen is a blank/loading state - only the global Back is available).
        2. If a domain ``actionable_step_check`` was injected, it must also agree
           this screen presents a genuine step for the current flow. This is what
           stops a transient "loading accounts" splash - which happens to carry an
           incidental clickable "Terms of use" link - from being treated as
           actionable and handed to the Brain, where the model would otherwise
           choose a diagnostic Back because no login/onboarding control is visible
           yet. When the check is absent, behaviour is the purely generic rule.
        """
        if not self._has_actionable_ui(available_actions):
            return False
        if self._actionable_step_check is not None:
            return bool(self._actionable_step_check(observation))
        return True

    def _terminal_failure_reason(self, observation: UIObservation) -> "str | None":
        """Return a domain evaluator's explicit terminal failure, if any."""
        failure_reason = getattr(self._goal_evaluator, "failure_reason", None)
        if not callable(failure_reason):
            return None
        reason = failure_reason(observation)
        return str(reason) if reason else None

    def _reobserve_before_action(self) -> UIObservation:
        reobserve = getattr(self._observer, "reobserve", None)
        if callable(reobserve):
            return reobserve()
        return self._observer.observe()

    @classmethod
    def _rebind_current_action(
        cls,
        action: Action,
        original: UIObservation,
        fresh: UIObservation,
    ) -> "Action | None":
        """Return the decision retargeted to the fresh pre-action observation,
        or None if the screen meaningfully changed / the target is unresolvable.

        ``element_id`` is a positional handle over the raw view hierarchy: the
        top-level window ORDER can differ between two observations of the SAME
        screen, so an identical control may carry a different id each observe
        (seen as e:0.1.* one observe, e:0.0.* the next). Re-locating the target
        by its stable identity - resource id, selector text and interaction
        traits - keeps an otherwise-current decision from being thrown away as
        "stale", and rebinds it to the id that exists NOW so the executor taps
        the right node instead of failing to find a phantom id.
        """
        if cls._meaningful_fingerprint(original) != cls._meaningful_fingerprint(
            fresh
        ):
            return None
        if action.kind == ActionKind.PRESS_BACK:
            return action
        original_target = original.find(action.target_id)
        if original_target is None:
            return None
        fresh_target = fresh.find(action.target_id)
        if fresh_target is not None and cls._same_target(
            original_target, fresh_target
        ):
            return action
        relocated = cls._relocate_target(original_target, fresh)
        if relocated is None:
            return None
        if relocated.element_id == action.target_id:
            return action
        return replace(action, target_id=relocated.element_id)

    @staticmethod
    def _target_identity(element: UIElement) -> tuple:
        """Stable, position-independent signature identifying a target element.

        Excludes ``element_id`` (a volatile hierarchy path) on purpose so the
        same control matches across a window-order reshuffle."""
        return (
            element.resource_id,
            element.selector_text,
            element.clickable,
            element.is_input,
            element.enabled,
        )

    @classmethod
    def _same_target(cls, first: UIElement, second: UIElement) -> bool:
        return cls._target_identity(first) == cls._target_identity(second)

    @classmethod
    def _relocate_target(
        cls, original_target: UIElement, fresh: UIObservation
    ) -> "UIElement | None":
        """Find the same target in ``fresh`` by stable identity.

        Requires a UNIQUE identity match: if the trait signature is ambiguous
        (or absent) in the fresh observation, return None so the decision is
        treated as stale rather than risk tapping the wrong element."""
        identity = cls._target_identity(original_target)
        matches = [
            element
            for element in fresh.elements
            if cls._target_identity(element) == identity
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    @staticmethod
    def _has_actionable_ui(available_actions) -> bool:
        """True if any available action targets a UI element (not just Back).

        SafetyValidator.available_actions always appends a single global
        PRESS_BACK. A result containing ONLY that means the observation exposed
        no actionable element - a blank/loading/transition screen - and the
        Brain must not be asked to invent an action against it.
        """
        return any(
            action.kind != ActionKind.PRESS_BACK for action in available_actions
        )

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
    def _meaningful_fingerprint(observation: UIObservation) -> "Counter":
        """An order-independent signature of the meaningful UI state.

        Non-secret, like ``_observation_fingerprint``, but limited to elements
        with a resource id or that are interactive (clickable/input). Decorative,
        id-less, non-interactive text (volatile clocks/animation) is excluded, so
        such noise does not reset the stuck counter.

        Returned as a multiset (``Counter``) rather than an ordered tuple so a
        pure re-ordering of the same windows/elements - which can happen between
        two observations of the SAME screen (see ``_rebind_current_action``) - is
        NOT treated as a change, while any genuine add/remove/trait change still
        is (multiplicity is preserved).
        """
        return Counter(
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

    def _offer_actions(self, observation, filled_keys, filled_fingerprint):
        """Safe actions for this screen, withholding already-entered credential
        fields until that credential field leaves the UI.

        A credential input whose field was already filled on this same screen is
        removed, so the model advances to submit instead of re-typing a field
        that still looks empty. Credential memory follows the stable field key
        itself rather than the whole-screen fingerprint: transient Android
        overlays such as Autofill can appear/disappear without making the
        password field new. A key is forgotten only after its field is absent,
        so returning to a later password screen can legitimately enter it again.
        Withholding is skipped if it would leave no actionable (non-Back) step,
        so the agent is never stranded on a screen whose only action was the
        already-entered field.
        """
        current_fingerprint = self._meaningful_fingerprint(observation)
        actions = self._safety_validator.available_actions(observation)
        present_credential_keys = {
            self._credential_field_key(action, observation)
            for action in actions
            if action.kind == ActionKind.INPUT_TEXT
            and action.credential_kind is not None
        }
        filled_keys.intersection_update(present_credential_keys)
        if filled_keys:
            withheld = tuple(
                action
                for action in actions
                if not self._is_entered_credential(action, observation, filled_keys)
            )
            if self._has_actionable_ui(withheld):
                actions = withheld
        return actions, current_fingerprint

    def _is_entered_credential(self, action, observation, filled_keys) -> bool:
        if action.kind != ActionKind.INPUT_TEXT or action.credential_kind is None:
            return False
        return self._credential_field_key(action, observation) in filled_keys

    @staticmethod
    def _credential_field_key(action, observation) -> tuple[str, str]:
        """Stable per-field key for a credential input.

        Keyed on the field's resource id (stable across identical re-renders,
        unlike the element path, which can shift), so an entered field is
        recognised again on the unchanged screen.
        """
        target = observation.find(action.target_id)
        field_id = (
            target.resource_id
            if target and target.resource_id
            else action.target_id
        )
        return (action.credential_kind.value, field_id or "")



def _default_max_actions() -> int:
    """Resolve the default action bound from the environment, else the constant.

    Only a POSITIVE integer is a meaningful bound. A non-positive value (0 or
    negative) is invalid config - like a non-integer - and falls back to the
    default rather than reaching the agent: a negative bound makes the action
    loop run zero times and crash (``range(max_actions + 1)`` is empty ->
    ``AssertionError``), and a zero bound makes every run fail immediately. The
    explicit ``--max-actions`` CLI flag still hard-errors on ``< 1`` at its own
    boundary; this only governs the env-derived default.
    """
    raw = os.environ.get("APPPILOT_MAX_ACTIONS")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return value
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
        value = value.strip()
        # Strip at most ONE matching surrounding quote pair (both ends the same
        # quote char). The previous greedy ``.strip('"').strip("'")`` removed any
        # number of both quote types from both ends, corrupting values whose
        # content legitimately begins/ends with the other quote (e.g.
        # ``"abc'"`` -> ``abc`` or ``"'x'"`` -> ``x``). Whitespace inside the
        # quotes is preserved; an unquoted value keeps its own quote characters.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
