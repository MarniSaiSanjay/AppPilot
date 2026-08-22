"""Cached login flows bound to resolved credential profiles."""

from __future__ import annotations

from collections.abc import Callable, Mapping

try:  # package-relative vs top-level (src on sys.path)
    from ...apppilot.models import RuntimeContext
    from ..credentials import (
        CredentialConfigurationError,
        CredentialProfile,
        normalize_profile_key,
    )
except ImportError:
    from apppilot.models import RuntimeContext
    from shared.credentials import (
        CredentialConfigurationError,
        CredentialProfile,
        normalize_profile_key,
    )

from .flow import LoginCapability, SharedLoginFlow


class ProfiledLoginFlowFactory:
    """Build one reusable login flow per resolved credential profile."""

    def __init__(
        self,
        profiles: Mapping[str, CredentialProfile],
        agent_builder: Callable[[RuntimeContext], object],
        *,
        flow_builder: Callable[[object], LoginCapability] = SharedLoginFlow,
    ) -> None:
        self._profiles = dict(profiles)
        self._agent_builder = agent_builder
        self._flow_builder = flow_builder
        self._flows: dict[str, LoginCapability] = {}

    def for_license(self, license_name: str) -> LoginCapability:
        key = normalize_profile_key(license_name)
        profile = self._profiles.get(key)
        if profile is None:
            raise CredentialConfigurationError(
                f"No resolved credential profile is available for License "
                f"{license_name!r} (profile key {key!r})"
            )
        flow = self._flows.get(key)
        if flow is None:
            agent = self._agent_builder(profile.runtime_context)
            flow = self._flow_builder(agent)
            self._flows[key] = flow
        return flow
