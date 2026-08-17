"""Entry point for the AppPilot setup / readiness command (``/init``).

Runnable as both ``python3 src/apppilot_init.py`` and
``python -m src.apppilot_init``. The implementation lives in the
``apppilot.init`` package; this module only wires up the entry point.
"""

from __future__ import annotations

try:  # package-relative (python -m src.apppilot_init)
    from .apppilot.init import main
except ImportError:  # top-level (src on sys.path)
    from apppilot.init import main


if __name__ == "__main__":
    raise SystemExit(main())
