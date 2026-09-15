"""Interactive readiness support for Entra-authenticated model access."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .model_client import (
    AUTH_ENTRA,
    ChatModelClient,
    ModelTransportError,
    verify_entra_model_authentication,
)


@dataclass(frozen=True)
class EntraReadiness:
    """Whether the selected model configuration requires and has Entra access."""

    required: bool
    ready: bool
    detail: str = ""


def _confirm_login(
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> bool:
    for _ in range(3):
        try:
            answer = input_fn(
                "Microsoft Entra sign-in is required for the model. "
                "Sign in to Azure now [Y/n]: "
            )
        except EOFError:
            return False
        answer = answer.strip().lower()
        if not answer or answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        output("Please answer y or n.")
    return False


def _current_subscription(
    runner: Callable[..., subprocess.CompletedProcess],
) -> str | None:
    """Read the current Azure CLI subscription without changing it."""
    try:
        result = runner(
            ["az", "account", "show", "--query", "id", "--output", "tsv"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    subscription = result.stdout.strip()
    return subscription or None


def _restore_subscription(
    subscription: str | None,
    runner: Callable[..., subprocess.CompletedProcess],
) -> str | None:
    """Restore the prior CLI subscription after a user-approved sign-in."""
    if subscription is None:
        return None
    try:
        result = runner(
            ["az", "account", "set", "--subscription", subscription],
            check=False,
        )
    except OSError:
        result = None
    if result is None or result.returncode != 0:
        return (
            "Azure sign-in succeeded, but AppPilot could not restore the prior "
            "Azure CLI subscription. Run `az account set --subscription "
            f"{subscription}` before using Azure CLI management commands."
        )
    return None


def ensure_entra_model_readiness(
    *,
    env: Mapping[str, str],
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    verifier: Callable[[], None] = verify_entra_model_authentication,
) -> EntraReadiness:
    """Check Entra model access and optionally perform a user-approved login.

    The selected Azure subscription is deliberately never changed: model
    inference needs an Entra token and resource RBAC, not a CLI default
    subscription. The browser/device-code flow remains entirely Azure CLI's.
    """
    config = ChatModelClient.config_from_env(env)
    if not config or config["auth_mode"] != AUTH_ENTRA:
        return EntraReadiness(required=False, ready=True)

    try:
        verifier()
        return EntraReadiness(required=True, ready=True)
    except ModelTransportError:
        pass

    remediation = (
        "Sign in to the work Azure account with `az login`, then retry. "
        "That account also needs Azure OpenAI access."
    )
    if not interactive:
        return EntraReadiness(required=True, ready=False, detail=remediation)
    if not _confirm_login(input_fn, output):
        return EntraReadiness(required=True, ready=False, detail=remediation)
    if which("az") is None:
        return EntraReadiness(
            required=True,
            ready=False,
            detail="Azure CLI (`az`) is not installed. Install it, sign in, then retry.",
        )

    previous_subscription = _current_subscription(runner)
    output("Opening the Azure CLI sign-in flow in your browser...")
    tenant_id = env.get("APPPILOT_ENTRA_TENANT_ID", "").strip()
    login_command = ["az", "login"]
    login_environment: dict[str, str] | None = None
    if tenant_id:
        # Keep Azure CLI's subscription selector off for this login process only.
        # AppPilot requires an Entra token, not a selected subscription.
        login_command.extend(["--tenant", tenant_id, "--output", "none"])
        login_environment = {
            **os.environ,
            "AZURE_CORE_LOGIN_EXPERIENCE_V2": "off",
        }
    try:
        if login_environment is None:
            result = runner(login_command, check=False)
        else:
            result = runner(login_command, check=False, env=login_environment)
    except OSError:
        return EntraReadiness(
            required=True,
            ready=False,
            detail="Azure CLI sign-in could not be started. Run `az login` manually, then retry.",
        )
    if result.returncode != 0:
        return EntraReadiness(
            required=True,
            ready=False,
            detail="Azure CLI sign-in was not completed. Run `az login`, then retry.",
        )
    restoration_error = _restore_subscription(previous_subscription, runner)
    if restoration_error is not None:
        return EntraReadiness(
            required=True,
            ready=False,
            detail=restoration_error,
        )

    try:
        verifier()
    except ModelTransportError:
        return EntraReadiness(required=True, ready=False, detail=remediation)
    return EntraReadiness(required=True, ready=True)
