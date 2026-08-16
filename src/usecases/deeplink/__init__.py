"""Deeplink use case: authoritative public API.

Data-driven deeplink regression suite. Composes the generic shared nodes
(``shared.login``, ``shared.installer``, ``shared.warmup``,
``shared.model_client``) and the ``apppilot`` framework primitives; owns the
deeplink testcase schema, verification, retry, results and CLI.

This module is the single source of the Deeplink public surface. The
``deeplink_runner`` and ``flows.deeplink`` compatibility facades re-export from
here, so existing imports and ``python -m`` entry points keep working.
"""

from __future__ import annotations

try:  # package-relative (python -m src.usecases.deeplink) vs top-level
    from ...shared.installer import AppInstaller, LocalApkInstaller
    from ...shared.warmup import MaestroWarmUp, WarmUp
    from ...shared.login import LoginCapability, SharedLoginFlow
except ImportError:  # top-level (src on sys.path, e.g. via the compat shim)
    from shared.installer import AppInstaller, LocalApkInstaller
    from shared.warmup import MaestroWarmUp, WarmUp
    from shared.login import LoginCapability, SharedLoginFlow

from .testcases import DeeplinkTestCase, load_deeplink_cases
from .verification import (
    ExpectationJudge,
    ExpectationJudgeOperationalError,
    ExpectationVerdict,
    LLMExpectationJudge,
)
from .results import AttemptResult, SuiteReport, TestCaseResult
from .runner import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_WAIT_SECONDS,
    DEFAULT_SETTLE_SECONDS,
    DeeplinkTestRunner,
)
from .orchestrator import DeeplinkSuiteOrchestrator
from .cli import main

# The orchestrator owns the suite's identity; re-export its name as the
# package-level ``SUITE_NAME`` so consumers (reporting, email, facades) have a
# single public handle without reaching into the orchestrator class.
SUITE_NAME = DeeplinkSuiteOrchestrator.SUITE_NAME

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_WAIT_SECONDS",
    "DEFAULT_SETTLE_SECONDS",
    "AttemptResult",
    "AppInstaller",
    "DeeplinkTestCase",
    "DeeplinkTestRunner",
    "DeeplinkSuiteOrchestrator",
    "LoginCapability",
    "LocalApkInstaller",
    "SharedLoginFlow",
    "ExpectationJudge",
    "ExpectationJudgeOperationalError",
    "ExpectationVerdict",
    "LLMExpectationJudge",
    "MaestroWarmUp",
    "SuiteReport",
    "TestCaseResult",
    "SUITE_NAME",
    "WarmUp",
    "load_deeplink_cases",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
