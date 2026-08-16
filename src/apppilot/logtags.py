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
    """Render a tag name as a bracketed prefix, e.g. ``"[LOGIN]"`` (empty for
    an empty ``name``, so callers can opt out of tagging)."""
    return f"[{name}]" if name else ""


def prefix(name: str, text: str) -> str:
    """Prefix ``text`` with the bracketed tag (and a space), or return ``text``
    unchanged when ``name`` is empty."""
    rendered = tag(name)
    return f"{rendered} {text}" if rendered else text


def trace(message: str, log_tag: str = "") -> None:
    """Emit one execution-trace line to stdout (a dumb printer, not narration).

    The single generic trace helper shared by every node/use case. Each call
    site sits immediately next to a REAL operation (a launch, a stop, an
    open_link, an install, a login, a judge, ...) so the terminal output is a
    faithful chronological record of what actually executed: a "done" line is
    only emitted after the underlying call returns. Secrets and full deeplink
    URLs are never passed in here.
    """
    print(prefix(log_tag, message))
