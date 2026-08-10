"""Tiny persistent key/value store for AppPilot setup answers.

One responsibility: remember the paths the operator had to tell us once (their
Office Mobile enlistment, their test-cases workbook) so preflight never asks
again on later runs. Backed by a small JSON file under the user's home
(``~/.apppilot/config.json`` by default; overridable for tests).

Deliberately forgiving: a missing or corrupt file reads as "no saved values"
rather than raising, so a bad config never blocks a run - at worst the operator
is prompted again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATH = Path.home() / ".apppilot" / "config.json"


class ConfigStore:
    """Load-on-read / write-through JSON store of string values by key."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self._path = Path(path)

    def _load(self) -> "dict[str, str]":
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        # Only keep flat string values; ignore anything unexpected.
        return {
            str(key): value
            for key, value in (data or {}).items()
            if isinstance(value, str)
        }

    def get(self, key: str) -> Optional[str]:
        """Return the saved value for ``key``, or None if unset/unreadable."""
        return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        """Persist ``value`` under ``key`` (best-effort; never raises)."""
        data = self._load()
        data[key] = value
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            # Persistence is a convenience; if the home dir is not writable we
            # simply prompt again next time rather than failing the run.
            pass
