"""Shared client for OpenAI-compatible chat completions.

Consolidates the endpoint configuration (environment resolution) and the HTTP
transport (Bearer-auth POST + JSON parse) that the model decision provider, the
deeplink expectation judge, and the login goal evaluator all previously
duplicated. It knows nothing about Login, Deeplink, FRI, or any use case - only
how to reach the model endpoint. Callers build the payload and interpret the
parsed response.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Mapping


DEFAULT_BASE_URL = "https://api.openai.com/v1"


class ModelTransportError(RuntimeError):
    """Raised when the model HTTP transport fails (network / decode error).

    Subclasses RuntimeError so existing callers that catch RuntimeError keep
    working; a consumer may re-map it to its own operational error type while
    preserving the original message (``str(error)``)."""


class ChatModelClient:
    """Minimal OpenAI-compatible chat client owning endpoint config + transport.

    Nothing about prompts, response interpretation, or any use case lives here.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def config_from_env(env: "Mapping[str, str] | None" = None) -> "dict | None":
        """Resolve ``{model, api_key, base_url}`` from the environment, or None.

        Reads ``APPPILOT_MODEL_API_KEY`` (required), ``APPPILOT_MODEL``
        (required), and ``APPPILOT_MODEL_BASE_URL`` (optional). Nothing is
        hardcoded; returns None when a required value is absent so callers can
        degrade to an honest unconfigured placeholder."""
        env = os.environ if env is None else env
        api_key = env.get("APPPILOT_MODEL_API_KEY")
        model = env.get("APPPILOT_MODEL")
        if not api_key or not model:
            return None
        base_url = env.get("APPPILOT_MODEL_BASE_URL") or DEFAULT_BASE_URL
        return {"model": model, "api_key": api_key, "base_url": base_url}

    def send(self, payload: dict) -> dict:
        """POST a chat-completions payload and return the parsed JSON response.

        Raises ModelTransportError on any network/decoding failure. The API key
        is sent only in the Authorization header and is never logged."""
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
        except (OSError, json.JSONDecodeError, UnicodeError) as error:
            raise ModelTransportError(str(error)) from error
