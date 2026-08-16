"""Resolve and persist the user-provided local APK path.

AppPilot installs ONLY a user-provided local APK - there is no build, no store
acquisition and no discovery. This module resolves the APK path (from an explicit
``--apk``, a previously saved path, or an interactive prompt), validates it, and
remembers the last good path so it can be offered as the default next time.

It deliberately reuses the same lightweight, file-based persistence philosophy as
the saved email recipient (bounded read, best-effort write, gitignored, no
secrets) rather than introducing a configuration framework. The APK is never
parsed here: structural validity is left to the Android/adb install path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

# Last good APK path is remembered here (gitignored) so it can be offered as the
# default next time - mirrors the saved email recipient store.
_APK_STORE = Path(__file__).resolve().parents[2] / ".apppilot_apk"


def validate_apk_path(raw: str | Path) -> Path:
    """Normalize and validate a user-supplied APK path.

    Expands ``~`` and resolves to an absolute path, then requires it to exist, be
    a regular file, carry a ``.apk`` suffix (case-insensitive) and be readable.
    Raises ``ValueError`` with a concise, non-sensitive reason otherwise. The APK
    is NOT parsed here - a structurally invalid but readable ``.apk`` is left for
    the adb install step to reject."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ValueError("APK path is empty")
    try:
        path = Path(text).expanduser().resolve()
    except OSError as error:
        raise ValueError(f"could not resolve APK path: {error}") from error
    if not path.exists():
        raise ValueError(f"APK path does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"APK path is not a file: {path}")
    if path.suffix.lower() != ".apk":
        raise ValueError(f"APK path is not a .apk file: {path}")
    if not os.access(path, os.R_OK):
        raise ValueError(f"APK path is not readable: {path}")
    return path


def _load_saved_apk_path() -> Optional[str]:
    try:
        # Bounded read so a corrupt/oversized store can never raise or blow up
        # memory; errors="replace" neutralizes non-UTF-8 content.
        with _APK_STORE.open("rb") as handle:
            saved = handle.read(4096).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return saved or None


def _save_apk_path(path: Path) -> None:
    try:
        _APK_STORE.write_text(str(path).strip() + "\n", encoding="utf-8")
    except OSError:
        pass  # Persisting the default is best-effort; never block the run.


def resolve_apk_path(
    cli_apk: str | None = None,
    *,
    interactive: bool = True,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    max_attempts: int = 3,
) -> Optional[Path]:
    """Resolve the validated local APK path to install, or ``None`` to abort.

    Precedence:
      1. An explicit ``cli_apk`` (``--apk``) is authoritative - validated and, if
         valid, remembered; if invalid, ``None`` is returned (clean setup error).
      2. Otherwise, non-interactive runs reuse a saved, still-valid path (or fail
         cleanly if there is none).
      3. Otherwise the operator is prompted with a single bracketed-default line
         (``APK path [<saved>]:``, mirroring the saved email recipient UX): Enter
         keeps the saved default, typing a path replaces it. Bounded attempts so
         an invalid path can never loop forever, and the saved default is only
         overwritten once a NEW valid path is supplied.

    Returns an absolute, validated ``Path`` or ``None`` (the caller then exits 2).
    Never raises: Ctrl-C / EOF at a prompt is treated as a clean abort."""

    def _ask(prompt: str) -> Optional[str]:
        try:
            return input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    # 1. Explicit --apk is authoritative in every mode.
    if cli_apk is not None and str(cli_apk).strip():
        try:
            path = validate_apk_path(cli_apk)
        except ValueError as error:
            output(f"Invalid APK path: {error}")
            return None
        _save_apk_path(path)
        return path

    saved = _load_saved_apk_path()

    # 2. Non-interactive: only a saved, still-valid path can be reused.
    if not interactive:
        if saved is None:
            output("no APK path configured; pass --apk PATH")
            return None
        try:
            return validate_apk_path(saved)
        except ValueError as error:
            output(f"Saved APK path is no longer usable: {error}")
            return None

    # 3. Interactive: a single bracketed-default prompt (no Yes/No step). Enter
    #    keeps the saved default unchanged; typing a valid path replaces it.
    for _ in range(max(1, max_attempts)):
        hint = f" [{saved}]" if saved else ""
        entered = _ask(f"APK path{hint}: ")
        if entered is None:  # Ctrl-C / EOF: abort.
            return None
        entered = entered.strip()
        if not entered:
            if saved is None:
                output("Invalid APK path: APK path is empty")
                continue
            try:  # Enter keeps the saved default; never re-saved/overwritten.
                return validate_apk_path(saved)
            except ValueError as error:
                output(f"Saved APK path is no longer valid: {error}")
                continue
        try:
            path = validate_apk_path(entered)
        except ValueError as error:
            output(f"Invalid APK path: {error}")
            continue  # invalid entry never overwrites the saved default
        _save_apk_path(path)  # replace the saved default with the new valid path
        return path

    output("no valid APK path provided")
    return None
