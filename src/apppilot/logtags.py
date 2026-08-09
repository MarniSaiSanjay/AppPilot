"""Central registry of log tags used across AppPilot.

Every user-facing log line is prefixed with a bracketed subsystem tag (e.g.
``[LOGIN]``, ``[BUILD]``). One registry keeps output greppable and gives new
components a consistent place to register a tag. String constants only.
"""

from __future__ import annotations

# Build / packaging of the app under test.
BUILD = "BUILD"

# The shared login/onboarding agent (authentication is a PRECONDITION of a
# deeplink test, never the test's own PASS/FAIL).
LOGIN = "LOGIN"

# Deeplink test suite lifecycle. Finer-grained phase tags below all belong to
# the DEEPLINK subsystem so a reader can tell which part ran.
DEEPLINK = "DEEPLINK"
SUITE = "SUITE"
INSTALLED = "INSTALLED"
UNINSTALLED = "UNINSTALLED"
INSTALLED_BATCH = "INSTALLED BATCH"
INSTALL = "INSTALL"
VERIFY = "VERIFY"
WARM_UP = "WARM-UP"

# The generic AppPilot agent when no caller-supplied context tag is set. Empty
# means "emit the original untagged lines" (preserves generic reuse).
AGENT = "AGENT"


def tag(name: str) -> str:
    """Render a tag name as a bracketed prefix, e.g. ``"[LOGIN]"`` (empty for
    an empty ``name``, so callers can opt out of tagging)."""
    return f"[{name}]" if name else ""


def prefix(name: str, text: str) -> str:
    """Prefix ``text`` with the bracketed tag (and a space), or return ``text``
    unchanged when ``name`` is empty."""
    rendered = tag(name)
    return f"{rendered} {text}" if rendered else text
