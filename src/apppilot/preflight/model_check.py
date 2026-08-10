"""Model-configuration check node.

One responsibility: confirm the evaluation model (the LLM judge that decides
whether each deeplink landed on the right screen) is configured before the suite
starts. Without it the judge cannot be built and every verification fails, so we
check the required environment variables up front and return a structured,
deterministic verdict. Pure/read-only (inspects a mapping), never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Environment variables the LLM judge needs (base URL is optional, defaulted).
REQUIRED_ENV_VARS = ("APPPILOT_MODEL", "APPPILOT_MODEL_API_KEY")
_OPTIONAL_BASE_URL_VAR = "APPPILOT_MODEL_BASE_URL"


@dataclass(frozen=True)
class ModelCheckResult:
    """Whether the evaluation model is configured, plus an actionable message."""

    ok: bool
    message: str


def check_model_configured(env: Mapping[str, str]) -> ModelCheckResult:
    """Return whether the evaluation model's required env vars are all set."""
    missing = [name for name in REQUIRED_ENV_VARS if not env.get(name)]
    if missing:
        return ModelCheckResult(
            False,
            "no evaluation model configured. Set "
            + " and ".join(REQUIRED_ENV_VARS)
            + f" (and optionally {_OPTIONAL_BASE_URL_VAR}).",
        )
    return ModelCheckResult(True, "evaluation model configured.")
