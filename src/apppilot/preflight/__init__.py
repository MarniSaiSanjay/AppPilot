"""Preflight package - the single "are we ready to run?" gate.

Small single-responsibility nodes that verify every hard prerequisite BEFORE
the suite touches the app, composed by :func:`run_preflight`:

  * ``python_check``        - the interpreter is new enough
  * ``maestro_check``       - the Maestro CLI is installed
  * ``model_check``         - the evaluation model env vars are set
  * ``credentials_check``   - sign-in credential env vars are set (advisory)
  * ``apk_source``          - choose build / existing / playstore (confirmed
                              each run); makes a local build optional
  * ``build_tools_check``   - JDK 17 + the ``omrdroid`` CLI are available
                              (only when building locally)
  * ``path_setup``          - resolve/remember required paths (test cases,
                              Office Mobile enlistment, prebuilt APK)
  * ``device_check``        - a usable adb device is resolved
  * ``emulator_autostart``  - start an AVD when nothing is connected
  * ``config_store``        - persist setup answers between runs
  * ``preflight_check``     - runs the above in order and returns one verdict

Importers only need :func:`run_preflight` and :class:`PreflightResult`; both are
re-exported here so callers can ``from apppilot import preflight`` and use
``preflight.run_preflight(...)``.
"""

from __future__ import annotations

from .preflight_check import PreflightResult, run_preflight

__all__ = ["PreflightResult", "run_preflight"]
