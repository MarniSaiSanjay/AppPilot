"""Entry point for the AppPilot setup / readiness command."""

from __future__ import annotations

try:
    from .apppilot.init import main
except ImportError:
    from apppilot.init import main


if __name__ == "__main__":
    raise SystemExit(main())
