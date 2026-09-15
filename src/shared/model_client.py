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
import logging
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from ipaddress import ip_address
from typing import Callable, Mapping
from urllib.parse import urlparse

from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider


DEFAULT_BASE_URL = "https://api.openai.com/v1"
AZURE_OPENAI_SCOPE = "https://ai.azure.com/.default"
AUTH_API_KEY = "api_key"
AUTH_ENTRA = "entra"
_AZURE_IDENTITY_CHAIN_LOGGER = logging.getLogger(
    "azure.identity._credentials.chained"
)
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


def verify_entra_model_authentication() -> None:
    """Acquire an Azure AI token without sending a model request."""
    # A signed-out Azure CLI is an expected interactive state. Azure Identity
    # logs every unavailable credential in its chain at warning level, while
    # AppPilot presents a concise sign-in prompt below.
    logger_was_disabled = _AZURE_IDENTITY_CHAIN_LOGGER.disabled
    _AZURE_IDENTITY_CHAIN_LOGGER.disabled = True
    try:
        try:
            token = DefaultAzureCredential().get_token(AZURE_OPENAI_SCOPE)
        except AzureError as error:
            raise ModelTransportError(
                "Microsoft Entra model authentication failed"
            ) from error
    finally:
        _AZURE_IDENTITY_CHAIN_LOGGER.disabled = logger_was_disabled
    if not token.token.strip():
        raise ModelTransportError("model authentication returned an empty token")


def validate_entra_base_url(base_url: str) -> str:
    """Require the Azure OpenAI v1 endpoint before sending an Entra token."""
    value = validate_base_url(base_url)
    parsed = urlparse(value)
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    if not hostname.endswith(".openai.azure.com"):
        raise ModelConfigurationError(
            "APPPILOT_MODEL_BASE_URL must use an Azure OpenAI "
            "*.openai.azure.com endpoint when APPPILOT_MODEL_AUTH=entra"
        )
    if parsed.path.rstrip("/") != "/openai/v1":
        raise ModelConfigurationError(
            "APPPILOT_MODEL_BASE_URL must end with /openai/v1 when "
            "APPPILOT_MODEL_AUTH=entra"
        )
    return value


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
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        auth_mode: str = AUTH_API_KEY,
        token_provider: "Callable[[], str] | None" = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ModelConfigurationError(
                "model timeout must be a finite positive number"
            )
        normalized_auth_mode = self._normalize_auth_mode(auth_mode)
        if normalized_auth_mode == AUTH_API_KEY:
            if not api_key.strip():
                raise ModelConfigurationError(
                    "APPPILOT_MODEL_API_KEY is required for api_key authentication"
                )
            if token_provider is not None:
                raise ModelConfigurationError(
                    "token_provider is only valid for entra authentication"
                )
            self._token_provider = None
        else:
            if api_key.strip():
                raise ModelConfigurationError(
                    "APPPILOT_MODEL_API_KEY must be blank for entra authentication"
                )
            self._token_provider = token_provider or get_bearer_token_provider(
                DefaultAzureCredential(), AZURE_OPENAI_SCOPE
            )
        self._model = model
        self._api_key = api_key
        self._auth_mode = normalized_auth_mode
        self._base_url = (
            validate_entra_base_url(base_url)
            if normalized_auth_mode == AUTH_ENTRA
            else validate_base_url(base_url)
        )
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def _normalize_auth_mode(auth_mode: str) -> str:
        normalized = (auth_mode or AUTH_API_KEY).strip().lower().replace("-", "_")
        if normalized not in {AUTH_API_KEY, AUTH_ENTRA}:
            raise ModelConfigurationError(
                "APPPILOT_MODEL_AUTH must be 'api_key' or 'entra'"
            )
        return normalized

    @staticmethod
    def config_from_env(env: "Mapping[str, str] | None" = None) -> "dict | None":
        """Resolve model endpoint and authentication configuration, or None.

        ``APPPILOT_MODEL_AUTH`` selects ``api_key`` (the backward-compatible
        default) or ``entra``. Entra mode uses ``DefaultAzureCredential`` and
        therefore requires no model API key. Returns None when the model name or
        the selected mode's required configuration is absent."""
        env = os.environ if env is None else env
        auth_mode = ChatModelClient._normalize_auth_mode(
            env.get("APPPILOT_MODEL_AUTH") or AUTH_API_KEY
        )
        api_key = env.get("APPPILOT_MODEL_API_KEY") or ""
        model = env.get("APPPILOT_MODEL")
        if not model or (auth_mode == AUTH_API_KEY and not api_key):
            return None
        if auth_mode == AUTH_ENTRA and api_key.strip():
            raise ModelConfigurationError(
                "APPPILOT_MODEL_API_KEY must be blank when "
                "APPPILOT_MODEL_AUTH=entra"
            )
        base_url_source = env.get("APPPILOT_MODEL_BASE_URL") or DEFAULT_BASE_URL
        base_url = (
            validate_entra_base_url(base_url_source)
            if auth_mode == AUTH_ENTRA
            else validate_base_url(base_url_source)
        )
        return {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "auth_mode": auth_mode,
        }

    def _refresh_authentication(self) -> None:
        if self._auth_mode != AUTH_ENTRA:
            return
        try:
            token = self._token_provider()
        except AzureError as error:
            raise ModelTransportError(
                "Microsoft Entra model authentication failed"
            ) from error
        if not token or not token.strip():
            raise ModelTransportError("model authentication returned an empty token")
        self._api_key = token

    def send(self, payload: dict) -> dict:
        """POST a chat-completions payload and return the parsed JSON response.

        Retries this safe, read-only request after transient transport, service,
        or throttling failures. Retry-After is honored with a bounded
        exponential floor. Non-retryable HTTP and response-decoding failures are
        surfaced immediately. The selected credential is sent only in the
        Authorization header and is never logged."""
        self._refresh_authentication()
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
