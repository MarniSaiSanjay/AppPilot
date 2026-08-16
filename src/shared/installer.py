"""Shared, use-case-agnostic device install/open capability.

A generic Android capability node: install the locally built APK via adb, ensure
the app is absent, and open it to the foreground (either via a deterministic adb
LAUNCHER intent or by tapping a store "Open" button through Maestro). It composes
only the generic Maestro executor primitives and knows nothing about Login,
Deeplink, FRI, or any use case. The caller decides which open mode to use.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

try:  # package-relative (python -m src.shared.installer) vs top-level
    from ..apppilot.android import MaestroExecutor
    from ..apppilot import logtags
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from apppilot.android import MaestroExecutor
    from apppilot import logtags

# Bounded wait for the app to become foreground after an adb launch (a returning
# launch command is not proof); confirmed via the deterministic adb foreground check.
DEFAULT_FOREGROUND_TIMEOUT_SECONDS = 30.0
DEFAULT_FOREGROUND_POLL_SECONDS = 1.0


class AppInstaller(Protocol):
    def ensure_absent(self) -> bool:
        ...

    def install_fresh(self) -> None:
        ...

    def open(self, via_store_button: bool = False) -> None:
        ...

    def install_and_open(self, via_store_button: bool = False) -> None:
        ...


class LocalApkInstaller:
    """Deterministic install of the locally built APK via adb, then open.

    Replaces installing from a store: we ``adb install`` the local build
    directly. ``open`` has two modes, and neither re-issues any pending intent
    (the app recovers a pending launch/link itself on start): the default
    launches via a deterministic adb LAUNCHER intent (no store window);
    ``via_store_button=True`` taps a store "Open" button through Maestro (used
    when the flow lands on the store window). Both confirm the app is foreground
    via a deterministic adb check before handing on. No coordinate taps, no LLM.
    The caller decides which mode to use.
    """

    def __init__(
        self,
        executor: MaestroExecutor,
        apk_path: str,
        *,
        foreground_timeout_seconds: float = DEFAULT_FOREGROUND_TIMEOUT_SECONDS,
        foreground_poll_seconds: float = DEFAULT_FOREGROUND_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executor = executor
        self._apk_path = apk_path
        self._foreground_timeout_seconds = max(0.0, foreground_timeout_seconds)
        self._foreground_poll_seconds = max(0.0, foreground_poll_seconds)
        self._sleep = sleep
        self._monotonic = monotonic

    def ensure_absent(self) -> bool:
        return self._executor.ensure_uninstalled()

    def install_fresh(self) -> None:
        logtags.trace(
            f"installing local build: {self._apk_path}", logtags.INSTALL
        )
        self._executor.install_apk(self._apk_path)

    def install_and_open(self, via_store_button: bool = False) -> None:
        self.install_fresh()
        self.open(via_store_button)

    def open(self, via_store_button: bool = False) -> None:
        # via_store_button taps a store "Open" button (used when the flow lands
        # on the store window); otherwise launch via a deterministic adb LAUNCHER
        # intent (no store window). Neither re-issues any pending intent. Return
        # once foreground.
        if via_store_button:
            logtags.trace(
                "tapping store Open button via Maestro", logtags.INSTALL
            )
            launch = self._executor.launch_app_via_open_btn_click
        else:
            logtags.trace("launching app via adb", logtags.INSTALL)
            launch = self._executor.launch_app_via_adb
        launch()
        self._wait_until_foreground(launch)

    def _wait_until_foreground(self, relaunch: Callable[[], None]) -> None:
        logtags.trace(
            "waiting for target app to become foreground", logtags.INSTALL
        )
        deadline = self._monotonic() + self._foreground_timeout_seconds
        while True:
            if self._executor.is_foreground():
                logtags.trace("target app is foreground", logtags.INSTALL)
                return
            if self._monotonic() >= deadline:
                raise RuntimeError(
                    logtags.prefix(
                        logtags.INSTALL,
                        "target app did not become foreground within timeout",
                    )
                )
            self._sleep(self._foreground_poll_seconds)
            # The first launch can be missed while the store window is still
            # settling; re-launch (best-effort) and poll again.
            try:
                relaunch()
            except RuntimeError:
                pass
