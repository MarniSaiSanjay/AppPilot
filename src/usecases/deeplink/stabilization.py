"""Installed account-group stabilization configuration."""

from __future__ import annotations

import time
from collections.abc import Callable

try:  # package-relative vs top-level (src on sys.path)
    from ...apppilot.android import MaestroExecutor
    from ...shared.warmup import (
        DEFAULT_WARM_UP_SETTLE_SECONDS,
        MaestroWarmUp,
    )
except ImportError:
    from apppilot.android import MaestroExecutor
    from shared.warmup import DEFAULT_WARM_UP_SETTLE_SECONDS, MaestroWarmUp


LICENSE_GROUP_STABILIZATION_CYCLES = 2


def build_license_group_stabilizer(
    executor: MaestroExecutor,
    *,
    settle_seconds: float = DEFAULT_WARM_UP_SETTLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> MaestroWarmUp:
    """Build the exact two-cycle stabilizer used after account preparation."""
    return MaestroWarmUp(
        executor,
        launches=LICENSE_GROUP_STABILIZATION_CYCLES,
        settle_seconds=settle_seconds,
        sleep=sleep,
    )
