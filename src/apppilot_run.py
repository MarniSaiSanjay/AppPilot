"""AppPilot ``/run`` entry point.

Selects a use case (auto when only one exists, else a numbered prompt) and hands
off to that use case's own CLI ``main``, which owns test-suite selection and runs
all of its test cases. This runner adds no test logic - it only dispatches.

Usage::

    python3 src/apppilot_run.py                 # pick a use case, then a suite
    python3 src/apppilot_run.py --usecase deeplink [use-case args...]

Args after use-case resolution (e.g. ``--excel``) pass through unchanged.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Sequence

_MAX_ATTEMPTS = 5  # bound the reprompt so bad input can't loop forever


def _usecases_dir() -> Path:
    return Path(__file__).resolve().parent / "usecases"


def _discover_usecases(directory: Path) -> list[str]:
    """Runnable use-case names (sorted): sub-packages exposing a ``cli`` module."""
    if not directory.is_dir():
        return []
    return sorted(
        (e.name for e in directory.iterdir()
         if (e / "__init__.py").is_file() and (e / "cli.py").is_file()),
        key=str.casefold,
    )


def _select_usecase(explicit, *, directory, interactive, output, input_fn=input):
    """Resolve which use case to run: explicit ``--usecase`` wins (validated);
    else none -> None, one -> auto, several -> prompt (or None non-interactive)."""
    usecases = _discover_usecases(directory)
    if explicit is not None:
        if explicit in usecases:
            return explicit
        output(f"ERROR: unknown use case {explicit!r}; available: "
                + (", ".join(usecases) or "none"))
        return None
    if not usecases:
        output(f"ERROR: no use cases found in {directory}")
        return None
    if len(usecases) == 1:
        output(f"Found 1 use case: {usecases[0]}")
        output(f"Running use case: {usecases[0]}")
        return usecases[0]
    if not interactive:
        output("ERROR: multiple use cases found; pass --usecase NAME to choose one")
        return None
    output("Available use cases:")
    for i, name in enumerate(usecases, 1):
        output(f"{i}. {name}")
    for _ in range(_MAX_ATTEMPTS):
        try:
            raw = input_fn("Select use case [1]: ").strip()
        except EOFError:
            return usecases[0]
        if not raw:
            return usecases[0]
        if raw.isdigit() and 1 <= int(raw) <= len(usecases):
            return usecases[int(raw) - 1]
        output(f"Invalid selection {raw!r}; enter a number 1-{len(usecases)}.")
    return None


def _load_usecase_main(name: str):
    try:  # package-relative (python -m src.apppilot_run)
        module = importlib.import_module(f".usecases.{name}.cli", package=__package__)
    except (ImportError, TypeError):  # top-level (src on sys.path)
        module = importlib.import_module(f"usecases.{name}.cli")
    return module.main


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)  # rest belongs to the use case
    parser.add_argument("--usecase", default=None)
    namespace, passthrough = parser.parse_known_args(argv)

    usecase = _select_usecase(
        namespace.usecase,
        directory=_usecases_dir(),
        interactive=getattr(sys.stdin, "isatty", lambda: False)(),
        output=lambda message: print(message, file=sys.stderr),
    )
    if usecase is None:
        return 2
    return _load_usecase_main(usecase)(passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
