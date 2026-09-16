"""Entry point for one-time local AppPilot prerequisite setup."""

from __future__ import annotations

try:
    from .apppilot.init import setup_main
except ImportError:
    from apppilot.init import setup_main


if __name__ == "__main__":
    raise SystemExit(setup_main())
