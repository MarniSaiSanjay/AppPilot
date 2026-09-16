"""AppPilot ``/run`` entry point."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path
from typing import Sequence

try:
    from .apppilot.agent import _load_dotenv
    from .shared.entra_auth import ensure_entra_model_readiness
    from .shared.model_client import ModelConfigurationError
except ImportError:
    from apppilot.agent import _load_dotenv
    from shared.entra_auth import ensure_entra_model_readiness
    from shared.model_client import ModelConfigurationError

_MAX_ATTEMPTS = 5


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
    """Resolve which use case to run."""
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
    try:
        module = importlib.import_module(f".usecases.{name}.cli", package=__package__)
    except (ImportError, TypeError):
        module = importlib.import_module(f"usecases.{name}.cli")
    return module.main


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--usecase", default=None)
    namespace, passthrough = parser.parse_known_args(argv)
    interactive = getattr(sys.stdin, "isatty", lambda: False)()
    if "--help" not in passthrough and "-h" not in passthrough:
        try:
            entra_readiness = ensure_entra_model_readiness(
                env=os.environ,
                interactive=interactive,
                output=lambda message: print(message, file=sys.stderr),
            )
        except ModelConfigurationError as error:
            print(f"ERROR: invalid model configuration: {error}", file=sys.stderr)
            return 2
        if entra_readiness.required and not entra_readiness.ready:
            print(
                f"ERROR: Microsoft Entra model authentication unavailable. "
                f"{entra_readiness.detail}",
                file=sys.stderr,
            )
            return 2

    usecase = _select_usecase(
        namespace.usecase,
        directory=_usecases_dir(),
        interactive=interactive,
        output=lambda message: print(message, file=sys.stderr),
    )
    if usecase is None:
        return 2
    return _load_usecase_main(usecase)(passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
