"""Backward-compatible facade for the deeplink runner.

The implementation now lives under ``usecases.deeplink`` (authoritative public
surface: ``usecases.deeplink.__init__``). This module re-exports the public
names so existing imports (and ``python -m src.deeplink_runner``) keep working
unchanged.
"""

from __future__ import annotations

try:  # package-relative (python -m src.deeplink_runner)
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
except ImportError:  # top-level (src on sys.path)
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


if __name__ == "__main__":
    raise SystemExit(main())
