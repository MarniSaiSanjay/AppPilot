"""AppPilot decision brain: model providers and decision contracts."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from .models import Action, ActionKind, CredentialKind, ExecutionContext, UIObservation


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

