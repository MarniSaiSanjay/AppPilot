"""Central registry and rendering helpers for subsystem log tags."""

from __future__ import annotations

BUILD = "BUILD"
EMAIL = "EMAIL"
INSTALL = "INSTALL"
INSTALLED = "INSTALLED"
INSTALLED_BATCH = "INSTALLED BATCH"
LOGIN = "LOGIN"
MAESTRO = "MAESTRO"
REPORT = "REPORT"
SUITE = "SUITE"
UNINSTALLED = "UNINSTALLED"
VERIFY = "VERIFY"
WARM_UP = "WARM-UP"


def tag(name: str) -> str:
    """Render a tag name as a bracketed prefix, e.g. ``"[LOGIN]"`` (empty for
    an empty ``name``, so callers can opt out of tagging)."""
    return f"[{name}]" if name else ""


def prefix(name: str, text: str) -> str:
    """Prefix ``text`` with the bracketed tag (and a space), or return ``text``
    unchanged when ``name`` is empty."""
    rendered = tag(name)
    return f"{rendered} {text}" if rendered else text
