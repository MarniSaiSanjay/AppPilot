"""Required-path setup node (resolve once, remember forever).

One responsibility: turn "AppPilot needs a path (a directory or a file) that we
cannot auto-detect" into a resolved, validated path - asking the operator only
when we truly have to, and persisting their answer so we never ask again.

Resolution order (first match wins):

  1. An explicit **override** (a path the operator supplied this run via a CLI
     flag or env var). If valid it wins over any saved value AND replaces it -
     "if you give a new one, we simply update it". If given but invalid we fail
     loudly rather than silently fall back to a stale saved path.
  2. A previously **saved** value (from :class:`config_store.ConfigStore`) that
     still points at something valid.
  3. The **default** candidate, when it is valid (not persisted - defaults are
     recomputed each run).
  4. **Prompt** the operator (only when interactive), validate what they type,
     save it, and use it.

When nothing is valid and we cannot prompt (non-interactive), it fails with an
actionable message instead of hanging. Never raises (Ctrl-C / EOF at a prompt
end the attempts cleanly rather than propagating).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config_store import ConfigStore

# prompter(prompt_text) -> the operator's raw reply. Injectable so tests need no
# real stdin; defaults to the builtin input().
Prompter = Callable[[str], str]

_MAX_PROMPT_ATTEMPTS = 3


@dataclass(frozen=True)
class PathResolution:
    """Outcome of resolving a required path.

    ``path`` is the resolved absolute path (only when ``ok``); ``message``
    explains the outcome either way.
    """

    ok: bool
    path: Optional[str] = None
    message: str = ""


def _is_valid(path: str, kind: str) -> bool:
    candidate = Path(path).expanduser()
    return candidate.is_dir() if kind == "dir" else candidate.is_file()


def resolve_required_path(
    *,
    key: str,
    label: str,
    default: Optional[str],
    kind: str,
    store: ConfigStore,
    override: Optional[str] = None,
    interactive: bool = False,
    prompter: Prompter = input,
) -> PathResolution:
    """Resolve the path for ``key``. See the module docstring for the order.

    ``label`` is a human name for the path (used in prompts/messages); ``kind``
    is ``"dir"`` or ``"file"`` and selects the existence check. ``override`` is
    a path the operator explicitly supplied this run (CLI flag / env var); when
    present it takes precedence over the saved value and is persisted.
    """
    noun = "directory" if kind == "dir" else "file"

    # 1) An explicit override supplied this run wins and updates the saved value.
    if override:
        expanded = str(Path(override).expanduser())
        if _is_valid(expanded, kind):
            store.set(key, expanded)
            return PathResolution(True, expanded, f"{label}: {expanded} (updated)")
        # A path was explicitly requested but does not exist: surface it rather
        # than silently using a stale saved value the operator meant to replace.
        return PathResolution(
            False,
            message=(
                f"{label} was set to '{override}' but no {noun} exists there - "
                "correct the path and re-run."
            ),
        )

    # 2) A saved answer that still resolves.
    saved = store.get(key)
    if saved and _is_valid(saved, kind):
        return PathResolution(True, str(Path(saved).expanduser()), f"{label}: {saved}")

    # 3) The default candidate, when valid.
    if default and _is_valid(default, kind):
        return PathResolution(
            True, str(Path(default).expanduser()), f"{label}: {default}"
        )

    # 4) Ask the operator - but only if we can.
    if not interactive:
        hint = f" (looked for {default})" if default else ""
        return PathResolution(
            False,
            message=(
                f"{label} not found{hint}. Provide the {noun} path and re-run "
                f"interactively, or set '{key}' in the AppPilot config."
            ),
        )

    for _ in range(_MAX_PROMPT_ATTEMPTS):
        try:
            reply = prompter(f"Enter the {label} ({noun} path): ").strip()
        except (EOFError, KeyboardInterrupt):
            # Aborting a prompt ends the attempts cleanly; run_preflight must
            # never raise, so we fall through to the actionable failure below.
            break
        if not reply:
            continue
        expanded = str(Path(reply).expanduser())
        if _is_valid(expanded, kind):
            store.set(key, expanded)
            return PathResolution(True, expanded, f"{label}: {expanded} (saved)")
        print(f"[PREFLIGHT] no {noun} at '{reply}' - try again.")

    return PathResolution(
        False,
        message=(
            f"{label} was not provided or does not exist. Set '{key}' in the "
            "AppPilot config or re-run and enter a valid path."
        ),
    )
