"""Shared device install/open capability."""

from __future__ import annotations

import math
import time
from typing import Callable, Protocol

try:
    from ..apppilot.android import AndroidOperationalError, MaestroExecutor
    from ..apppilot import logtags
except ImportError:
    from apppilot.android import AndroidOperationalError, MaestroExecutor
    from apppilot import logtags

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
    """Install a local APK via adb, then open the app."""

    def __init__(
        self,
        executor: MaestroExecutor,
        apk_path: str,
        *,
        pre_install_check: Callable[[str], None] | None = None,
        post_install_check: Callable[[], None] | None = None,
        foreground_timeout_seconds: float = DEFAULT_FOREGROUND_TIMEOUT_SECONDS,
        foreground_poll_seconds: float = DEFAULT_FOREGROUND_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not math.isfinite(foreground_timeout_seconds)
            or foreground_timeout_seconds < 0
        ):
            raise ValueError(
                "foreground_timeout_seconds must be a finite non-negative number"
            )
        if (
            not math.isfinite(foreground_poll_seconds)
            or foreground_poll_seconds < 0
        ):
            raise ValueError(
                "foreground_poll_seconds must be a finite non-negative number"
            )
        self._executor = executor
        self._apk_path = apk_path
        self._pre_install_check = pre_install_check
        self._post_install_check = post_install_check
        self._foreground_timeout_seconds = foreground_timeout_seconds
        self._foreground_poll_seconds = foreground_poll_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def ensure_absent(self) -> bool:
        return self._executor.ensure_uninstalled()

    def install_fresh(self) -> None:
        logtags.trace(
            f"installing local build: {self._apk_path}", logtags.INSTALL
        )
        if self._pre_install_check is not None:
            self._pre_install_check(self._apk_path)
        self._executor.install_apk(self._apk_path)
        if self._post_install_check is not None:
            self._post_install_check()

    def install_and_open(self, via_store_button: bool = False) -> None:
        self.install_fresh()
        self.open(via_store_button)

    def open(self, via_store_button: bool = False) -> None:
        if via_store_button and self._executor.is_foreground():
            return
        if via_store_button:
            logtags.trace(
                "tapping store Open button via Maestro", logtags.INSTALL
            )
            launch = self._executor.launch_app_via_open_btn_click
        else:
            logtags.trace("launching app via adb", logtags.INSTALL)
            launch = self._executor.launch_app_via_adb
        try:
            launch()
        except AndroidOperationalError as error:
            if via_store_button:
                try:
                    if self._executor.is_foreground():
                        return
                except RuntimeError:
                    pass
            raise error
        self._wait_until_foreground(launch)

    def _wait_until_foreground(
        self, relaunch: Callable[[], None]
    ) -> None:
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
            try:
                relaunch()
            except RuntimeError:
                pass
