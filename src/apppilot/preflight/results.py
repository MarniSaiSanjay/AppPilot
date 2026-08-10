"""Shared result type for simple preflight checks.

A check that has no data to return beyond "did it pass, and why" uses this
instead of hand-rolling an identical dataclass. Nodes that carry extra data
(e.g. the resolved device serial or path) keep their own richer result types.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    """Whether a check passed, plus an actionable, operator-facing message."""

    ok: bool
    message: str
