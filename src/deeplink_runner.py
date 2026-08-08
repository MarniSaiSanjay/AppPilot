"""Backward-compatible facade for the deeplink runner.

The implementation now lives in ``flows.deeplink``. This module re-exports the
public names so existing imports (and ``python -m src.deeplink_runner``) keep
working unchanged.
"""

from __future__ import annotations

try:  # package-relative (python -m src.deeplink_runner)
    from .flows.deeplink import (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_RETRY_WAIT_SECONDS,
        DEFAULT_SETTLE_SECONDS,
        AttemptResult,
        DeeplinkTestCase,
        DeeplinkTestRunner,
        ExpectationJudge,
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
    from flows.deeplink import (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_RETRY_WAIT_SECONDS,
        DEFAULT_SETTLE_SECONDS,
        AttemptResult,
        DeeplinkTestCase,
        DeeplinkTestRunner,
        ExpectationJudge,
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
