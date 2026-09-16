"""Shared client for Microsoft Entra-authenticated Azure OpenAI chat completions."""

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


AZURE_OPENAI_SCOPE = "https://ai.azure.com/.default"
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
    """Raised when the model HTTP transport fails."""


class ModelConfigurationError(ValueError):
    """Model endpoint configuration is unsafe or malformed."""


def verify_entra_model_authentication() -> None:
    """Acquire an Azure AI token without sending a model request."""
    logger_was_disabled = _AZURE_IDENTITY_CHAIN_LOGGER.disabled
    _AZURE_IDENTITY_CHAIN_LOGGER.disabled = True
    try:
        try:
            token = DefaultAzureCredential().get_token(AZURE_OPENAI_SCOPE)  # allow-secret
        except AzureError as error:
            raise ModelTransportError(
                "Microsoft Entra model authentication failed"
            ) from error
    finally:
        _AZURE_IDENTITY_CHAIN_LOGGER.disabled = logger_was_disabled
    if not token.token.strip():
        raise ModelTransportError("model authentication returned an empty token")


def validate_azure_openai_base_url(base_url: str) -> str:
    """Require the Azure OpenAI v1 endpoint before sending an Entra token."""
    value = validate_base_url(base_url)
    parsed = urlparse(value)
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    if not hostname.endswith(".openai.azure.com"):
        raise ModelConfigurationError(
            "APPPILOT_MODEL_BASE_URL must use an Azure OpenAI "
            "*.openai.azure.com endpoint"
        )
    if parsed.path.rstrip("/") != "/openai/v1":
        raise ModelConfigurationError(
            "APPPILOT_MODEL_BASE_URL must end with /openai/v1 when "
            "using Azure OpenAI"
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
    """Microsoft Entra-authenticated Azure OpenAI chat client."""

    def __init__(
        self,
        model: str,
        base_url: str,
        timeout: float = 60.0,
        token_provider: "Callable[[], str] | None" = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ModelConfigurationError(
                "model timeout must be a finite positive number"
            )
        self._model = model
        self._token_provider = token_provider or get_bearer_token_provider(
            DefaultAzureCredential(), AZURE_OPENAI_SCOPE
        )
        self._access_token = ""
        self._base_url = validate_azure_openai_base_url(base_url)
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def config_from_env(env: "Mapping[str, str] | None" = None) -> "dict | None":
        """Resolve model endpoint and authentication configuration."""
        env = os.environ if env is None else env
        model = env.get("APPPILOT_MODEL")
        base_url_source = env.get("APPPILOT_MODEL_BASE_URL")
        if not model or not base_url_source:
            return None
        base_url = validate_azure_openai_base_url(base_url_source)
        return {
            "model": model,
            "base_url": base_url,
        }

    def _refresh_authentication(self) -> None:
        try:
            token = self._token_provider()
        except AzureError as error:
            raise ModelTransportError(
                "Microsoft Entra model authentication failed"
            ) from error
        if not token or not token.strip():
            raise ModelTransportError("model authentication returned an empty token")
        self._access_token = token

    def send(self, payload: dict) -> dict:
        """POST a chat-completions payload and return parsed JSON."""
        self._refresh_authentication()
        data = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._access_token}",
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
