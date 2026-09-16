"""Shared app warm-up capability."""

from __future__ import annotations

import time
from typing import Callable, Protocol

try:
    from ..apppilot.android import MaestroExecutor
    from ..apppilot import logtags
except ImportError:
    from apppilot.android import MaestroExecutor
    from apppilot import logtags

DEFAULT_WARM_UP_SETTLE_SECONDS = 3.0
DEFAULT_WARM_UP_LAUNCHES = 3


class WarmUp(Protocol):
    def __call__(self) -> None:
        ...


class MaestroWarmUp:
    """Default first-install warm-up."""

    def __init__(
        self,
        executor: MaestroExecutor,
        launches: int = DEFAULT_WARM_UP_LAUNCHES,
        settle_seconds: float = DEFAULT_WARM_UP_SETTLE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._executor = executor
        self._launches = max(1, launches)
        self._settle_seconds = settle_seconds
        self._sleep = sleep

    def __call__(self) -> None:
        logtags.trace(
            f"Starting installed-app preparation: {self._launches} cycles",
            logtags.WARM_UP,
        )
        for index in range(self._launches):
            cycle = index + 1
            logtags.trace(
                f"Cycle {cycle}/{self._launches}: launch app", logtags.WARM_UP
            )
            self._executor.launch_app()
            if self._settle_seconds:
                logtags.trace(
                    f"Cycle {cycle}/{self._launches}: "
                    f"waiting {self._settle_seconds:g}s",
                    logtags.WARM_UP,
                )
                self._sleep(self._settle_seconds)
            logtags.trace(
                f"Cycle {cycle}/{self._launches}: stop app", logtags.WARM_UP
            )
            self._executor.stop_app()
            logtags.trace(
                f"Cycle {cycle}/{self._launches} complete", logtags.WARM_UP
            )
        logtags.trace("Installed-app preparation complete", logtags.WARM_UP)
