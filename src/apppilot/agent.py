"""Generic AppPilot agent orchestration (observe -> decide -> validate -> act)."""

from __future__ import annotations

import math
import os
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Callable, TypeVar

from .android import MaestroExecutor, MaestroHierarchyObserver
from .brain import DecisionRequest, ModelDecisionProvider
from . import logtags
from .models import (
    Action,
    ActionKind,
    CredentialInputMethod,
    CredentialKind,
    CredentialVerificationStatus,
    ExecutionContext,
    GoalEvaluator,
    RuntimeContext,
    UIElement,
    UIObservation,
)
from .safety import SafetyValidator
from .work_defaults import load_work_defaults
from . import environment

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAX_ACTIONS = 30
DEFAULT_MAX_STUCK_ACTIONS = 5
DEFAULT_NONACTIONABLE_WAIT_SECONDS = 2.0
DEFAULT_MAX_NONACTIONABLE_WAITS = 10
DEFAULT_MAX_POST_SUBMIT_EMPTY_OBSERVATIONS = 5
DEFAULT_MAX_CREDENTIAL_REPEAT_SETTLES = 2
_T = TypeVar("_T")


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
        if max_actions < 1:
            raise ValueError("max_actions must be at least 1")
        if max_stuck_actions < 1:
            raise ValueError("max_stuck_actions must be at least 1")
        if (
            not math.isfinite(nonactionable_wait_seconds)
            or nonactionable_wait_seconds < 0
        ):
            raise ValueError(
                "nonactionable_wait_seconds must be a finite non-negative number"
            )
        if max_nonactionable_waits < 0:
            raise ValueError("max_nonactionable_waits must not be negative")
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
        self._actionable_step_check = actionable_step_check
        self._log_tag = log_tag
        self._timing_enabled = os.environ.get("APPPILOT_TIMING") == "1"
        self._last_failure_reason: str | None = None

    @property
    def last_failure_reason(self) -> str | None:
        return self._last_failure_reason

    def _emit(self, text: str) -> None:
        """Emit one detailed agent log entry when tracing is enabled."""
        logtags.trace(text, self._log_tag)

    def _log_goal_reached(self, reached: bool) -> None:
        self._emit(f"GOAL REACHED:\n{str(reached).lower()}\n")

    def _log_pass(self) -> None:
        self._emit("RESULT:\nPASS")

    def _log_fail(self, detail: str) -> None:
        self._last_failure_reason = detail
        self._emit(f"RESULT:\nFAIL - {detail}")

    def _timed_call(self, label: str, callback: "Callable[[], _T]") -> _T:
        if not self._timing_enabled:
            return callback()
        started = time.monotonic()
        try:
            return callback()
        finally:
            self._emit(
                f"TIMING:\n{label} {time.monotonic() - started:.3f}s\n"
            )

    def run(self, goal: str, guidance: str | None = None) -> bool:
        self._last_failure_reason = None
        begin = getattr(self._decision_provider, "begin_run", None)
        if callable(begin):
            begin()
        succeeded = False
        try:
            succeeded = self._run(goal, guidance)
            return succeeded
        finally:
            finish = getattr(self._decision_provider, "finish_run", None)
            if callable(finish):
                finish(succeeded)
            finish_goal = getattr(self._goal_evaluator, "finish_run", None)
            if callable(finish_goal):
                finish_goal(succeeded)

    def _run(self, goal: str, guidance: str | None = None) -> bool:
        reset_recovery = getattr(self._observer, "reset_recovery_budget", None)
        if callable(reset_recovery):
            reset_recovery()
        self._emit(f"GOAL:\n{goal}\n")
        if guidance:
            self._emit(f"GUIDANCE:\n{guidance}\n")
        self._emit(f"MAX ACTIONS:\n{self._max_actions}\n")
        self._emit(f"MAX STUCK ACTIONS:\n{self._max_stuck_actions}\n")

        history: list[str] = []
        last_credential_key: tuple[str, str] | None = None
        last_credential_fingerprint: tuple | None = None
        filled_credential_keys: set[tuple[str, str]] = set()
        filled_fingerprint: tuple | None = None
        last_acted_fingerprint: tuple | None = None
        consecutive_stuck = 0
        pending_fast_submit_fingerprint: tuple | None = None
        pending_fast_submit_identity: tuple[str, str, str] | None = None
        pending_fast_submit_waits = 0
        post_submit_transition_active = False
        post_submit_empty_observations = 0
        pending_credential_verification: CredentialKind | None = None
        recovered_credential_kinds: set[CredentialKind] = set()
        credential_repeat_settles = 0
        for step in range(self._max_actions + 1):
            observation = self._timed_call("observe", self._observer.observe)
            self._emit(f"OBSERVE:\n{observation.describe()}\n")

            verification = observation.credential_verification
            if pending_credential_verification is not None:
                if (
                    verification is None
                    or verification.kind != pending_credential_verification
                ):
                    self._log_fail(
                        "credential entry did not produce a local verification "
                        "result"
                    )
                    return False
                pending_credential_verification = None
            recovery_action = self._credential_recovery_action(observation)
            if (
                verification is not None
                and verification.status != CredentialVerificationStatus.MATCHED
                and recovery_action is None
                and (
                    verification.status
                    == CredentialVerificationStatus.MISMATCHED
                    or self._has_credential_field_of_kind(
                        observation,
                        verification.kind,
                    )
                )
            ):
                self._log_fail(
                    "credential verification failed and the input field could "
                    "not be uniquely recovered"
                )
                return False
            if recovery_action is not None:
                if step == self._max_actions:
                    self._log_fail(
                        f"action/step limit reached ({self._max_actions})"
                    )
                    return False
                if (
                    recovery_action.credential_kind
                    in recovered_credential_kinds
                ):
                    self._log_fail(
                        f"{recovery_action.credential_kind.value} input "
                        "verification failed after exact recovery"
                    )
                    return False
                if not self._runtime_context.has(recovery_action.credential_kind):
                    self._log_fail(
                        "credential verification failed and the configured "
                        "credential is unavailable"
                    )
                    return False
                try:
                    self._safety_validator.validate(
                        recovery_action,
                        observation,
                    )
                except ValueError as error:
                    self._emit(
                        "CREDENTIAL VERIFICATION:\n"
                        f"recovery rejected - {error}\n"
                    )
                    self._log_fail(
                        "credential verification failed and recovery was unsafe"
                    )
                    return False
                fallback = getattr(
                    self._executor,
                    "execute_credential_fallback",
                    None,
                )
                if not callable(fallback):
                    self._log_fail(
                        "credential verification failed and no exact recovery "
                        "path is available"
                    )
                    return False
                secret = self._runtime_context.resolve(
                    recovery_action.credential_kind
                )
                self._emit(
                    "CREDENTIAL VERIFICATION:\n"
                    f"{recovery_action.credential_kind.value} mismatch; "
                    "replacing the configured value through the exact local "
                    "clipboard path\n"
                )
                self._timed_call(
                    "credential recovery",
                    lambda: fallback(recovery_action, observation, secret),
                )
                if not self._arm_credential_verification(
                    recovery_action,
                    observation,
                    secret,
                ):
                    self._log_fail(
                        "exact credential recovery cannot be verified by the "
                        "configured observer"
                    )
                    return False
                pending_credential_verification = (
                    recovery_action.credential_kind
                )
                recovered_credential_kinds.add(
                    recovery_action.credential_kind
                )
                credential_key = self._credential_field_key(
                    recovery_action,
                    observation,
                )
                filled_credential_keys.add(credential_key)
                filled_fingerprint = self._meaningful_fingerprint(observation)
                last_credential_key = credential_key
                last_credential_fingerprint = self._observation_fingerprint(
                    observation
                )
                last_acted_fingerprint = filled_fingerprint
                history.append(recovery_action.describe(observation))
                continue

            reached = self._timed_call(
                "goal evaluation",
                lambda: self._goal_evaluator.is_reached(goal, observation),
            )
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
            if (
                post_submit_transition_active
                and self._has_relevant_ui(observation)
                and not self._has_credential_field(observation)
            ):
                post_submit_transition_active = False
                post_submit_empty_observations = 0
            waits = 0
            while not self._has_actionable_step(
                observation, available_actions
            ):
                if (
                    post_submit_transition_active
                    and not self._has_relevant_ui(observation)
                ):
                    post_submit_empty_observations += 1
                    if (
                        post_submit_empty_observations
                        >= DEFAULT_MAX_POST_SUBMIT_EMPTY_OBSERVATIONS
                    ):
                        self._log_fail(
                            "no actionable step appeared after "
                            f"{post_submit_empty_observations} wait(s); app stayed "
                            "in a loading/transition state with no "
                            "login/onboarding action to take after credential "
                            "submission"
                        )
                        return False
                elif self._has_relevant_ui(observation):
                    post_submit_empty_observations = 0
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
                observation = self._timed_call(
                    "observe after wait",
                    self._observer.observe,
                )
                self._emit(f"OBSERVE:\n{observation.describe()}\n")
                reached = self._timed_call(
                    "goal evaluation",
                    lambda: self._goal_evaluator.is_reached(goal, observation),
                )
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
                if (
                    post_submit_transition_active
                    and self._has_relevant_ui(observation)
                    and not self._has_credential_field(observation)
                ):
                    post_submit_transition_active = False
                    post_submit_empty_observations = 0

            if pending_fast_submit_waits > 0:
                current_fingerprint = self._meaningful_fingerprint(observation)
                if (
                    (
                        current_fingerprint == pending_fast_submit_fingerprint
                        or self._submitted_control_is_disabled(
                            observation,
                            pending_fast_submit_identity,
                        )
                    )
                    and self._has_credential_field(observation)
                ):
                    pending_fast_submit_waits -= 1
                    self._emit(
                        "WAIT:\nsubmitted credentials; waiting for authentication "
                        "transition\n"
                    )
                    self._sleep(0.5)
                    continue
                pending_fast_submit_fingerprint = None
                pending_fast_submit_identity = None
            pending_fast_submit_waits = 0

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
            decision = self._timed_call(
                "decision",
                lambda: self._decision_provider.decide(request),
            )

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

            current_action = decision.action
            if decision.reobserve_required:
                fresh_observation = self._timed_call(
                    "pre-action observe",
                    self._reobserve_before_action,
                )
                self._emit(
                    f"RE-OBSERVE:\n{fresh_observation.describe()}\n"
                )
                current_action = self._rebind_current_action(
                    current_action, observation, fresh_observation
                )
                if current_action is None:
                    self._emit(
                        "STALE DECISION:\ndiscarded because the UI changed before "
                        "execution\n"
                    )
                    continue
                observation = fresh_observation
            else:
                self._emit("FAST DECISION:\nusing the current observation\n")
            meaningful_fingerprint = self._meaningful_fingerprint(observation)

            try:
                self._safety_validator.validate(current_action, observation)
            except ValueError as error:
                self._emit(f"SAFETY VALIDATION:\nrejected - {error}\n")
                self._log_fail("model proposed an unsafe or invalid action")
                return False
            self._emit("SAFETY VALIDATION:\npassed\n")

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

                credential_key = self._credential_field_key(action, observation)
                fingerprint = self._observation_fingerprint(observation)
                if (
                    credential_key == last_credential_key
                    and fingerprint == last_credential_fingerprint
                ):
                    if (
                        credential_repeat_settles
                        < DEFAULT_MAX_CREDENTIAL_REPEAT_SETTLES
                    ):
                        credential_repeat_settles += 1
                        self._emit(
                            "CREDENTIAL:\n"
                            f"{action.credential_kind.value} already entered on "
                            "this screen; settling before re-observing "
                            f"({credential_repeat_settles}/"
                            f"{DEFAULT_MAX_CREDENTIAL_REPEAT_SETTLES})\n"
                        )
                        self._sleep(self._nonactionable_wait_seconds)
                        continue
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
                credential_repeat_settles = 0

            self._emit(f"ACTION:\n{action.describe(observation)}\n")
            execute = self._executor.execute
            if not decision.reobserve_required:
                fast_execute = getattr(self._executor, "execute_fast", None)
                if callable(fast_execute):
                    execute = fast_execute
            execution_result = self._timed_call(
                "action",
                lambda: execute(action, observation, secret=secret),
            )
            if (
                action.credential_kind is not None
                and secret is not None
                and execution_result
                in (
                    CredentialInputMethod.ADB,
                    CredentialInputMethod.CLIPBOARD,
                )
            ):
                if not self._arm_credential_verification(
                    action,
                    observation,
                    secret,
                ):
                    self._log_fail(
                        "credential entry cannot be verified by the "
                        "configured observer"
                    )
                    return False
                pending_credential_verification = action.credential_kind
            record_executed = getattr(
                self._decision_provider, "record_executed", None
            )
            if callable(record_executed):
                record_executed(action)
            if action.credential_kind is not None:
                filled_credential_keys.add(
                    self._credential_field_key(action, observation)
                )
                filled_fingerprint = meaningful_fingerprint
            history.append(action.describe(observation))
            last_acted_fingerprint = meaningful_fingerprint
            if (
                not decision.reobserve_required
                and self._is_submit_action(action, observation)
            ):
                pending_fast_submit_fingerprint = meaningful_fingerprint
                pending_fast_submit_identity = self._submit_identity(
                    action,
                    observation,
                )
                pending_fast_submit_waits = 2
                post_submit_transition_active = True
                post_submit_empty_observations = 0

        raise AssertionError("Agent loop exited unexpectedly")

    @staticmethod
    def _has_credential_field(observation: UIObservation) -> bool:
        return any(
            SafetyValidator.credential_kind(element) is not None
            for element in observation.elements
        )

    @staticmethod
    def _has_credential_field_of_kind(
        observation: UIObservation,
        kind: CredentialKind,
    ) -> bool:
        return any(
            SafetyValidator.credential_kind(element) == kind
            for element in observation.elements
        )

    @staticmethod
    def _credential_recovery_action(
        observation: UIObservation,
    ) -> Action | None:
        verification = observation.credential_verification
        if (
            verification is None
            or verification.status == CredentialVerificationStatus.MATCHED
        ):
            return None
        same_kind = [
            element
            for element in observation.elements
            if SafetyValidator.credential_kind(element) == verification.kind
        ]
        candidates = [
            element
            for element in same_kind
            if (
                element.element_id == verification.element_id
                or element.resource_id == verification.field_id
            )
        ]
        if (
            not candidates
            and verification.status == CredentialVerificationStatus.UNAVAILABLE
            and len(same_kind) == 1
        ):
            candidates = same_kind
        if len(candidates) != 1:
            return None
        return Action(
            ActionKind.INPUT_TEXT,
            target_id=candidates[0].element_id,
            credential_kind=verification.kind,
        )

    def _arm_credential_verification(
        self,
        action: Action,
        observation: UIObservation,
        secret: str,
    ) -> bool:
        if action.credential_kind is None:
            return False
        expect_credential = getattr(
            self._observer,
            "expect_credential",
            None,
        )
        if not callable(expect_credential):
            return False
        expect_credential(
            action.credential_kind,
            self._credential_field_key(action, observation)[1],
            secret,
        )
        return True

    @staticmethod
    def _has_relevant_ui(observation: UIObservation) -> bool:
        return any(
            element.label
            or element.resource_id
            or element.clickable
            or element.is_input
            for element in observation.elements
        )

    @staticmethod
    def _is_submit_action(
        action: Action,
        observation: UIObservation,
    ) -> bool:
        if action.kind != ActionKind.TAP:
            return False
        target = observation.find(action.target_id)
        if target is None:
            return False
        label = (target.own_text or target.label).strip().casefold()
        resource_id = target.resource_id.strip().casefold()
        return label in ("next", "sign in") or resource_id in (
            "nextbutton",
            "idsibutton9",
        )

    @staticmethod
    def _submit_identity(
        action: Action,
        observation: UIObservation,
    ) -> tuple[str, str, str] | None:
        target = observation.find(action.target_id)
        if target is None:
            return None
        label = (target.own_text or target.label).strip().casefold()
        return (
            target.resource_id.strip().casefold(),
            label,
            target.element_id,
        )

    @staticmethod
    def _submitted_control_is_disabled(
        observation: UIObservation,
        identity: tuple[str, str, str] | None,
    ) -> bool:
        if identity is None:
            return False
        resource_id, label, element_id = identity
        for element in observation.elements:
            if element.enabled:
                continue
            current_resource = element.resource_id.strip().casefold()
            current_label = (
                element.own_text or element.label
            ).strip().casefold()
            if resource_id and current_resource == resource_id:
                return True
            if element.element_id == element_id:
                return True
            if not resource_id and label and current_label == label:
                return True
        return False

    def _has_actionable_step(
        self,
        observation,
        available_actions,
    ) -> bool:
        """Return whether the observation presents an actionable step."""
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
        """Retarget a decision to a fresh observation."""
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
        """Return a stable target-element signature."""
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
        """Find the same target in a fresh observation."""
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
        """Return whether any available action targets a UI element."""
        return any(
            action.kind != ActionKind.PRESS_BACK for action in available_actions
        )

    @staticmethod
    def _observation_fingerprint(observation: UIObservation) -> tuple:
        """Return a stable screen signature."""
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
        """Return an order-independent signature of the meaningful UI state."""
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
        """Return safe actions for the current screen."""
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
        """Return a stable per-field key for a credential input."""
        target = observation.find(action.target_id)
        field_id = (
            target.resource_id
            if target and target.resource_id
            else action.target_id
        )
        return (action.credential_kind.value, field_id or "")



def _default_max_actions() -> int:
    """Resolve the default action bound."""
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
    """Resolve the stuck bound."""
    raw = os.environ.get("APPPILOT_MAX_STUCK_ACTIONS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_STUCK_ACTIONS



def _load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a local .env into os.environ."""
    env_path = path or (REPO_ROOT / ".env")
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    environment.add_java_path()
    environment.add_android_sdk_paths()
    environment.add_maestro_path()
    load_work_defaults()
