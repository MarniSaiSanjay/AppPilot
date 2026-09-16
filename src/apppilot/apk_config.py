"""Resolve and persist the user-provided local APK path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

_APK_STORE = Path(__file__).resolve().parents[2] / ".apppilot_apk"


def validate_apk_path(raw: str | Path) -> Path:
    """Normalize and validate a user-supplied APK path."""
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
        with _APK_STORE.open("rb") as handle:
            saved = handle.read(4096).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return saved or None


def _save_apk_path(path: Path) -> None:
    try:
        _APK_STORE.write_text(str(path).strip() + "\n", encoding="utf-8")
    except OSError:
        pass


def resolve_apk_path(
    cli_apk: str | None = None,
    *,
    interactive: bool = True,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    max_attempts: int = 3,
) -> Optional[Path]:
    """Resolve the validated local APK path to install."""

    def _ask(prompt: str) -> Optional[str]:
        try:
            return input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    if cli_apk is not None and str(cli_apk).strip():
        try:
            path = validate_apk_path(cli_apk)
        except ValueError as error:
            output(f"Invalid APK path: {error}")
            return None
        _save_apk_path(path)
        return path

    saved = _load_saved_apk_path()

    if not interactive:
        if saved is None:
            output("no APK path configured; pass --apk PATH")
            return None
        try:
            return validate_apk_path(saved)
        except ValueError as error:
            output(f"Saved APK path is no longer usable: {error}")
            return None

    for _ in range(max(1, max_attempts)):
        hint = f" [{saved}]" if saved else ""
        entered = _ask(f"APK path{hint}: ")
        if entered is None:
            return None
        entered = entered.strip()
        if not entered:
            if saved is None:
                output("Invalid APK path: APK path is empty")
                continue
            try:
                return validate_apk_path(saved)
            except ValueError as error:
                output(f"Saved APK path is no longer valid: {error}")
                continue
        try:
            path = validate_apk_path(entered)
        except ValueError as error:
            output(f"Invalid APK path: {error}")
            continue
        _save_apk_path(path)
        return path

    output("no valid APK path provided")
    return None
