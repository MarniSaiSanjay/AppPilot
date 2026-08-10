"""Sign-in credentials check node.

One responsibility: confirm the sign-in credentials the login flow needs are
configured, and tell the operator to set them when they are not.

Unlike the model/tooling checks this is a **warning, not a hard gate**: the
login flow is a no-op when the app is already signed in, so a run can legitimately
succeed with no credentials (e.g. a Play Store app that is already logged in).
Missing credentials only break test cases that actually require an interactive
sign-in - so we surface an actionable warning and let the run proceed rather than
blocking it. Pure/read-only (inspects a mapping), never raises.

Credentials are secrets: they are read from the environment only and are NEVER
prompted-for-and-persisted the way paths are (nothing secret is written to the
AppPilot config).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Environment variables the shared login flow reads (see models.RuntimeContext).
CREDENTIAL_ENV_VARS = ("APPPILOT_USERNAME", "APPPILOT_PASSWORD")


@dataclass(frozen=True)
class CredentialsCheckResult:
    """Whether sign-in credentials are configured, plus an operator message.

    ``configured`` is the real signal; ``ok`` is always True because missing
    credentials are a warning, not a fatal error (see the module docstring).
    """

    ok: bool
    configured: bool
    message: str


def check_credentials_configured(env: Mapping[str, str]) -> CredentialsCheckResult:
    """Return whether the sign-in credential env vars are set (never fatal)."""
    missing = [name for name in CREDENTIAL_ENV_VARS if not env.get(name)]
    if missing:
        return CredentialsCheckResult(
            True,
            False,
            "sign-in credentials not set (missing "
            + " and ".join(missing)
            + ") - test cases that require login will fail. Set "
            + " and ".join(CREDENTIAL_ENV_VARS)
            + " if your cases need sign-in.",
        )
    return CredentialsCheckResult(True, True, "sign-in credentials configured.")
