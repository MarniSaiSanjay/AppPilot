"""Central registry and rendering helpers for subsystem log tags."""

from __future__ import annotations

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
    """Render a tag name as a bracketed prefix."""
    return f"[{name}]" if name else ""


def prefix(name: str, text: str) -> str:
    """Prefix text with a bracketed tag when name is set."""
    rendered = tag(name)
    return f"{rendered} {text}" if rendered else text


def trace(message: str, log_tag: str = "") -> None:
    """Emit one execution-trace line to stdout."""
    print(prefix(log_tag, message))
