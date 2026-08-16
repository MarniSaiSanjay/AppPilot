"""AppPilot deeplink suite (compatibility shim).

The deeplink use case now lives under the ``usecases.deeplink`` package (its
authoritative public surface is ``usecases.deeplink.__init__``). This module
re-exports that surface so existing imports (``flows.deeplink``) and
``python -m flows.deeplink`` keep working unchanged.
"""

from __future__ import annotations

try:  # package-relative (python -m src.flows.deeplink) vs top-level
    from ..usecases.deeplink import (  # noqa: F401
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
        WarmUp,
        load_deeplink_cases,
        main,
    )
except ImportError:  # top-level (src on sys.path)
    from usecases.deeplink import (  # noqa: F401
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
        WarmUp,
        load_deeplink_cases,
        main,
    )


if __name__ == "__main__":
    raise SystemExit(main())
