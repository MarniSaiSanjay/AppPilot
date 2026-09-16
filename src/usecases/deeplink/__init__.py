"""Deeplink use case public API."""

from __future__ import annotations

try:
    from ...shared.installer import AppInstaller, LocalApkInstaller
    from ...shared.warmup import MaestroWarmUp, WarmUp
    from ...shared.login import LoginCapability, SharedLoginFlow
except ImportError:
    from shared.installer import AppInstaller, LocalApkInstaller
    from shared.warmup import MaestroWarmUp, WarmUp
    from shared.login import LoginCapability, SharedLoginFlow

from .deeplink_testcase_loader import DeeplinkTestCase, load_deeplink_cases
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
