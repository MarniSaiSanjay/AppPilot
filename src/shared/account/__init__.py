"""Shared contract for preparing an active app account session."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..credentials import CredentialProfile


class AccountPreparationKind(str, Enum):
    """How the requested account became active."""

    ALREADY_ACTIVE = "already_active"
    ADDED = "added"
    SWITCHED = "switched"


@dataclass(frozen=True)
class AccountPreparationResult:
    """Secret-free result returned by an account-session capability."""

    succeeded: bool
    kind: AccountPreparationKind | None = None
    reason: str = ""

    @classmethod
    def ready(
        cls, kind: AccountPreparationKind
    ) -> "AccountPreparationResult":
        return cls(succeeded=True, kind=kind)

    @classmethod
    def failed(cls, reason: str) -> "AccountPreparationResult":
        return cls(succeeded=False, reason=reason)


class AccountSessionCapability(Protocol):
    """Ensure that the app is ready under one resolved credential profile.

    Implementations must keep account identifiers local: raw account names and
    emails must not enter model requests, logs, exceptions, or returned results.
    """

    def ensure_active(
        self, profile: CredentialProfile
    ) -> AccountPreparationResult:
        ...
