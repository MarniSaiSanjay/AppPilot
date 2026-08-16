"""Deeplink expectation judge (use-case-specific semantic verification).

Decides whether the observed Android UI SEMANTICALLY satisfies a natural-language
Expected Result after a deeplink launch. The prompt and match/verdict semantics
are the Deeplink use case's own; the model transport is delegated to the generic
``shared.model_client.ChatModelClient`` (no provider/model/credential is
hardcoded). The judge is given ONLY the Expected Result and the redacted observed
UI; it never selects or alters a deeplink.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Protocol

try:  # package-relative (python -m src.usecases.deeplink.verification) vs top-level
    from ...apppilot.models import UIObservation
    from ...shared.model_client import ChatModelClient, ModelTransportError
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.models import UIObservation
    from shared.model_client import ChatModelClient, ModelTransportError


# --------------------------------------------------------------------------- #
# AI expectation judge (semantic expected-vs-observed evaluation)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExpectationVerdict:
    matched: bool
    reason: str


class ExpectationJudgeOperationalError(RuntimeError):
    """Expected model transport/response failure during verification."""


class ExpectationJudge(Protocol):
    def evaluate(
        self, expected_result: str, observation: UIObservation
    ) -> ExpectationVerdict:
        ...


class LLMExpectationJudge:
    """Model-backed semantic judge: does the observed UI satisfy Expected Result?

    Uses the same OpenAI-compatible configuration as the agent's decision model
    (``APPPILOT_MODEL``, ``APPPILOT_MODEL_API_KEY``, ``APPPILOT_MODEL_BASE_URL``)
    so no provider/model/credential is hardcoded. It is given ONLY the natural
    language Expected Result and the redacted observed UI; it decides match or
    mismatch. It never selects or alters a deeplink.
    """

    _SYSTEM_PROMPT = (
        "You are AppPilot's deeplink result judge. You are given an EXPECTED "
        "RESULT written in natural language (for example \"Chat screen\", \"Chat "
        "screen with prompt\", \"Chat screen with prompt \\\"<specific text>\\\"\", "
        "\"Researcher screen with prompt\", or an expected error/failure state) "
        "and the CURRENT observed Android UI after a deep link was launched.\n"
        "\n"
        "Decide whether the observed UI SEMANTICALLY satisfies the expected "
        "result. Judge the screen TYPE by meaning, not by exact wording or "
        "specific selectors, and do not rely on any single hardcoded label.\n"
        "\n"
        "RULES:\n"
        "- Match on the OBSERVED state, not on whether the deeplink 'succeeded'. "
        "If the expected result describes an error/failure state and that is what "
        "is observed, that is a MATCH.\n"
        "- 'with prompt' with NO specific text: a non-empty prompt/text must be "
        "present in the composer/input; its expected presence/absence must "
        "agree.\n"
        "- If the expected result SPECIFIES OR QUOTES a particular prompt, topic, "
        "or content, the observed composer/input must actually contain THAT SAME "
        "prompt (same meaning/topic) - not merely some text. A generic, "
        "placeholder, suggested, or DIFFERENT prompt is a MISMATCH. Minor "
        "wording/whitespace differences are acceptable; a different topic or a "
        "different prompt is NOT.\n"
        "- An initial suggested-prompt / welcome / onboarding screen that shows a "
        "random or different suggested prompt does NOT satisfy an expected "
        "specific prompt.\n"
        "- If the observed UI is ambiguous, incidental (a transient/loading or "
        "unrelated interruption), or clearly a different screen than expected, it "
        "is NOT a match.\n"
        "\n"
        "RESPOND with strict JSON only, no prose outside it:\n"
        '{"match": <true|false>, "reason": <string>}'
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
        self._client = ChatModelClient(
            model=model, api_key=api_key, base_url=base_url, timeout=timeout
        )
        self._transport = transport or self._http_transport

    @classmethod
    def from_env(cls, env: dict | None = None) -> "LLMExpectationJudge | None":
        config = ChatModelClient.config_from_env(env)
        if config is None:
            return None
        return cls(**config)

    def evaluate(
        self, expected_result: str, observation: UIObservation
    ) -> ExpectationVerdict:
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": self._render(expected_result, observation)},
            ],
            "response_format": {"type": "json_object"},
        }
        response = self._transport(payload)
        try:
            content = response["choices"][0]["message"]["content"]
            decoded = json.loads(content)
        except (KeyError, IndexError, ValueError, TypeError) as error:
            return ExpectationVerdict(
                matched=False, reason=f"Judge response could not be parsed: {error}"
            )
        matched = decoded.get("match")
        if type(matched) is not bool:
            return ExpectationVerdict(
                matched=False,
                reason="Judge response field 'match' must be a JSON boolean.",
            )
        reason = str(decoded.get("reason") or "").strip() or "(no reason given)"
        return ExpectationVerdict(matched=matched, reason=reason)

    @staticmethod
    def _render(expected_result: str, observation: UIObservation) -> str:
        # observation.describe() already redacts credential fields, so no secret
        # can reach the judge prompt.
        return (
            f"EXPECTED RESULT: {expected_result}\n"
            "CURRENT UI ELEMENTS:\n"
            f"{observation.describe(limit=40)}\n"
            'Respond with JSON: {"match": true|false, "reason": "..."}'
        )

    def _http_transport(self, payload: dict) -> dict:
        try:
            return self._client.send(payload)
        except ModelTransportError as error:
            raise ExpectationJudgeOperationalError(
                f"Judge request failed: {error}"
            ) from error
