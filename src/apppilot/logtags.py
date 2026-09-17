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

_TRACE_ENABLED = False


def tag(name: str) -> str:
    """Render a tag name as a bracketed prefix."""
    return f"[{name}]" if name else ""


def prefix(name: str, text: str) -> str:
    """Prefix text with a bracketed tag when name is set."""
    rendered = tag(name)
    return f"{rendered} {text}" if rendered else text


def set_trace_enabled(enabled: bool) -> None:
    """Enable or disable detailed execution tracing."""
    global _TRACE_ENABLED
    _TRACE_ENABLED = enabled


def trace(message: str, log_tag: str = "") -> None:
    """Emit one detailed execution-trace line when tracing is enabled."""
    if _TRACE_ENABLED:
        print(prefix(log_tag, message))
