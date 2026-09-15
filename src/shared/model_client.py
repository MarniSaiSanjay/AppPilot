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
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from ipaddress import ip_address
from typing import Mapping
from urllib.parse import urlparse


DEFAULT_BASE_URL = "https://api.openai.com/v1"
_MAX_SEND_ATTEMPTS = 3
_BASE_RETRY_WAIT_SECONDS = 2.0
_MAX_RETRY_WAIT_SECONDS = 30.0


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirects())


class ModelTransportError(RuntimeError):
    """Raised when the model HTTP transport fails (network / decode error).

    Subclasses RuntimeError so existing callers that catch RuntimeError keep
    working; a consumer may re-map it to its own operational error type while
    preserving the original message (``str(error)``)."""


class ModelConfigurationError(ValueError):
    """Model endpoint configuration is unsafe or malformed."""


def validate_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError as error:
        raise ModelConfigurationError(
            "APPPILOT_MODEL_BASE_URL must be a valid http(s) URL"
        ) from error
    if parsed.scheme not in ("http", "https") or not hostname:
        raise ModelConfigurationError(
            "APPPILOT_MODEL_BASE_URL must be a valid http(s) URL"
        )
    if username or password:
        raise ModelConfigurationError(
            "APPPILOT_MODEL_BASE_URL must not contain credentials"
        )
    if parsed.scheme == "https":
        return value
    hostname = hostname.casefold()
    local = hostname == "localhost" or hostname.endswith(".localhost")
    if not local:
        try:
            local = ip_address(hostname).is_loopback
        except ValueError:
            local = False
    if not local:
        raise ModelConfigurationError(
            "remote APPPILOT_MODEL_BASE_URL endpoints must use https"
        )
    return value


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
        if not math.isfinite(timeout) or timeout <= 0:
            raise ModelConfigurationError(
                "model timeout must be a finite positive number"
            )
        self._model = model
        self._api_key = api_key
        self._base_url = validate_base_url(base_url)
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
        base_url = validate_base_url(
            env.get("APPPILOT_MODEL_BASE_URL") or DEFAULT_BASE_URL
        )
        return {"model": model, "api_key": api_key, "base_url": base_url}

    def send(self, payload: dict) -> dict:
        """POST a chat-completions payload and return the parsed JSON response.

        Retries this safe, read-only request after transient transport, service,
        or throttling failures. Retry-After is honored with a bounded
        exponential floor. Non-retryable HTTP and response-decoding failures are
        surfaced immediately. The API key is sent only in the Authorization
        header and is never logged."""
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
        for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
            try:
                with _NO_REDIRECT_OPENER.open(
                    http_request, timeout=self._timeout
                ) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeError) as error:
                raise ModelTransportError(str(error)) from error
            except OSError as error:
                if attempt == _MAX_SEND_ATTEMPTS or not self._is_retryable(error):
                    detail = (
                        f"model timeout: {error}"
                        if isinstance(error, TimeoutError)
                        else str(error)
                    )
                    raise ModelTransportError(detail) from error
                wait_seconds = self._retry_wait_seconds(error, attempt)
                time.sleep(wait_seconds)

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        if isinstance(error, urllib.error.HTTPError):
            return error.code == 429 or 500 <= error.code < 600
        return True

    @staticmethod
    def _retry_wait_seconds(error: BaseException, attempt: int) -> float:
        fallback = min(
            _BASE_RETRY_WAIT_SECONDS * (2 ** (attempt - 1)),
            _MAX_RETRY_WAIT_SECONDS,
        )
        if not isinstance(error, urllib.error.HTTPError) or error.headers is None:
            return fallback

        retry_after = error.headers.get("Retry-After")
        if not retry_after:
            return fallback
        try:
            server_wait = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                server_wait = (
                    retry_at - datetime.now(timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return fallback
        return min(max(fallback, server_wait, 0.0), _MAX_RETRY_WAIT_SECONDS)
