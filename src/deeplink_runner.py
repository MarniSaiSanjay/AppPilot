"""Backward-compatible facade for the deeplink runner."""

from __future__ import annotations

try:
    from .usecases.deeplink import (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_RETRY_WAIT_SECONDS,
        DEFAULT_SETTLE_SECONDS,
        AttemptResult,
        AppInstaller,
        DeeplinkTestCase,
        DeeplinkTestRunner,
        DeeplinkSuiteOrchestrator,
        LoginCapability,
        LocalApkInstaller,
        SharedLoginFlow,
        ExpectationJudge,
        ExpectationJudgeOperationalError,
        ExpectationVerdict,
        LLMExpectationJudge,
        MaestroWarmUp,
        SuiteReport,
        TestCaseResult,
        SUITE_NAME,
        WarmUp,
        load_deeplink_cases,
        main,
    )
except ImportError:
    from usecases.deeplink import (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_RETRY_WAIT_SECONDS,
        DEFAULT_SETTLE_SECONDS,
        AttemptResult,
        AppInstaller,
        DeeplinkTestCase,
        DeeplinkTestRunner,
        DeeplinkSuiteOrchestrator,
        LoginCapability,
        LocalApkInstaller,
        SharedLoginFlow,
        ExpectationJudge,
        ExpectationJudgeOperationalError,
        ExpectationVerdict,
        LLMExpectationJudge,
        MaestroWarmUp,
        SuiteReport,
        TestCaseResult,
        SUITE_NAME,
        WarmUp,
        load_deeplink_cases,
        main,
    )

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_WAIT_SECONDS",
    "DEFAULT_SETTLE_SECONDS",
    "AttemptResult",
    "AppInstaller",
    "DeeplinkSuiteOrchestrator",
    "DeeplinkTestCase",
    "DeeplinkTestRunner",
    "ExpectationJudge",
    "ExpectationJudgeOperationalError",
    "ExpectationVerdict",
    "LLMExpectationJudge",
    "LocalApkInstaller",
    "LoginCapability",
    "MaestroWarmUp",
    "SUITE_NAME",
    "SharedLoginFlow",
    "SuiteReport",
    "TestCaseResult",
    "WarmUp",
    "load_deeplink_cases",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
