"""Secure credential profiles selected by a non-secret runtime label."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

try:  # package-relative vs top-level (src on sys.path)
    from ..apppilot.models import CredentialKind, RuntimeContext
except ImportError:
    from apppilot.models import CredentialKind, RuntimeContext


class CredentialConfigurationError(RuntimeError):
    """Raised when a credential profile cannot be selected safely."""


@dataclass(frozen=True)
class CredentialProfile:
    """A safe profile identity and its locally held credential context."""

    license_name: str
    key: str
    runtime_context: RuntimeContext


def normalize_profile_key(license_name: str) -> str:
    """Convert a workbook License value to a stable environment-variable key."""
    label = " ".join((license_name or "").split())
    key = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper()
    if not key:
        raise CredentialConfigurationError("License must contain letters or numbers")
    return key


class CredentialProfileResolver:
    """Resolve License labels to profile-specific credentials in the environment."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    def resolve(self, license_name: str) -> CredentialProfile:
        label = " ".join((license_name or "").split())
        key = normalize_profile_key(label)
        username_key = f"APPPILOT_USERNAME_{key}"
        password_key = f"APPPILOT_PASSWORD_{key}"
        username = self._env.get(username_key)
        password = self._env.get(password_key)
        missing = [
            variable
            for variable, value in (
                (username_key, username),
                (password_key, password),
            )
            if not value
        ]
        if missing:
            raise CredentialConfigurationError(
                f"Credential profile {label!r} is missing required environment "
                f"variable(s): {', '.join(missing)}"
            )
        return CredentialProfile(
            license_name=label,
            key=key,
            runtime_context=RuntimeContext(
                {
                    CredentialKind.USERNAME: username,
                    CredentialKind.PASSWORD: password,
                }
            ),
        )

    def resolve_all(
        self, license_names: Iterable[str]
    ) -> dict[str, CredentialProfile]:
        """Resolve unique profiles and reject ambiguous normalized labels."""
        labels_by_key: dict[str, tuple[str, str]] = {}
        ordered_keys: list[str] = []
        for license_name in license_names:
            label = " ".join((license_name or "").split())
            key = normalize_profile_key(label)
            identity = label.casefold()
            existing = labels_by_key.get(key)
            if existing is not None and existing[1] != identity:
                raise CredentialConfigurationError(
                    f"License values {existing[0]!r} and {label!r} both map to "
                    f"credential profile key {key!r}"
                )
            if existing is None:
                labels_by_key[key] = (label, identity)
                ordered_keys.append(key)
        return {
            key: self.resolve(labels_by_key[key][0])
            for key in ordered_keys
        }
