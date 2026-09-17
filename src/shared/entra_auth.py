"""Interactive readiness for Entra-authenticated Azure access (one az login)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .model_client import (
    ChatModelClient,
    ModelTransportError,
    verify_entra_model_authentication,
)

try:  # pragma: no cover - import shim for package vs. flat execution
    from ..apppilot.work_defaults import (
        RelayConfigurationError,
        load_relay_secrets,
    )
except ImportError:  # pragma: no cover - flat execution fallback
    from apppilot.work_defaults import (  # type: ignore[no-redef]
        RelayConfigurationError,
        load_relay_secrets,
    )


_MODEL_REMEDIATION = (
    "Sign in to the work Azure account with `az login`, then retry. "
    "That account also needs Azure OpenAI access."
)

# Set once an approved sign-in succeeds so later callers reuse it.
_azure_login_completed = False


def reset_azure_login_state() -> None:
    """Reset the process-level sign-in memo (tests)."""
    global _azure_login_completed
    _azure_login_completed = False


@dataclass(frozen=True)
class EntraReadiness:
    """Whether the selected model configuration requires and has Entra access."""

    required: bool
    ready: bool
    detail: str = ""


@dataclass(frozen=True)
class AzureLoginOutcome:
    """Shared Azure sign-in result; ``detail`` set only for blocking errors."""

    ok: bool
    detail: str | None = None


def _confirm_login(
    purpose: str,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> bool:
    prompt = (
        f"Microsoft Entra sign-in is required for {purpose}. "
        "Sign in to Azure now [Y/n]: "
    )
    for _ in range(3):
        try:
            answer = input_fn(prompt)
        except EOFError:
            return False
        answer = answer.strip().lower()
        if not answer or answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        output("Please answer y or n.")
    return False


def perform_interactive_azure_login(
    purpose: str,
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AzureLoginOutcome:
    """Run a single user-approved ``az login`` shared across AppPilot."""
    global _azure_login_completed
    if _azure_login_completed:
        return AzureLoginOutcome(ok=True)
    if not interactive:
        return AzureLoginOutcome(ok=False)
    if not _confirm_login(purpose, input_fn, output):
        return AzureLoginOutcome(ok=False)
    if which("az") is None:
        return AzureLoginOutcome(
            ok=False,
            detail="Azure CLI (`az`) is not installed. Install it, sign in, then retry.",
        )

    previous_subscription = _current_subscription(runner)
    output("Opening the Azure CLI sign-in flow in your browser...")
    try:
        result = runner(["az", "login"], check=False)
    except OSError:
        return AzureLoginOutcome(
            ok=False,
            detail="Azure CLI sign-in could not be started. Run `az login` manually, then retry.",
        )
    if result.returncode != 0:
        return AzureLoginOutcome(
            ok=False,
            detail="Azure CLI sign-in was not completed. Run `az login`, then retry.",
        )
    restoration_error = _restore_subscription(previous_subscription, runner)
    if restoration_error is not None:
        return AzureLoginOutcome(ok=False, detail=restoration_error)

    _azure_login_completed = True
    return AzureLoginOutcome(ok=True)


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
    """Check Entra model access and optionally perform a user-approved login."""
    config = ChatModelClient.config_from_env(env)
    if not config:
        return EntraReadiness(required=False, ready=True)

    try:
        verifier()
        return EntraReadiness(required=True, ready=True)
    except ModelTransportError:
        pass

    outcome = perform_interactive_azure_login(
        "the model",
        interactive=interactive,
        input_fn=input_fn,
        output=output,
        which=which,
        runner=runner,
    )
    if not outcome.ok:
        return EntraReadiness(
            required=True,
            ready=False,
            detail=outcome.detail or _MODEL_REMEDIATION,
        )

    try:
        verifier()
    except ModelTransportError:
        return EntraReadiness(
            required=True, ready=False, detail=_MODEL_REMEDIATION
        )
    return EntraReadiness(required=True, ready=True)


def ensure_relay_environment(
    base_env: Mapping[str, str],
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    loader: Callable[[], Mapping[str, str]] = load_relay_secrets,
) -> tuple[dict[str, str], str | None]:
    """Load relay secrets, offering the shared sign-in when needed."""
    try:
        return {**base_env, **loader()}, None
    except RelayConfigurationError as error:
        detail = str(error)

    outcome = perform_interactive_azure_login(
        "AppPilot email and telemetry reporting",
        interactive=interactive,
        input_fn=input_fn,
        output=output,
        which=which,
        runner=runner,
    )
    if not outcome.ok:
        return dict(base_env), outcome.detail or detail

    try:
        return {**base_env, **loader()}, None
    except RelayConfigurationError as error:
        return dict(base_env), str(error)
