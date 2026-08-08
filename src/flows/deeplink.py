"""Data-driven deeplink test runner for AppPilot.

This reuses the existing AppPilot abstractions - the same Maestro executor,
Maestro UI observer, and the same OpenAI-compatible model configuration - rather
than introducing a second agent framework. Its shape is deliberately narrow:

    load test cases (Excel)
        -> for each case: launch the EXACT deeplink (deterministic, Maestro)
            -> observe the resulting Android UI (deterministic, Maestro)
                -> AI judges whether the observed UI satisfies the natural
                   language Expected Result (semantic, model)
                    -> PASS, or kill + wait + retry (deterministic)

The deeplink and the Expected Result both come from the Excel and are used
verbatim. The model never invents, modifies, or chooses a deeplink; it only
evaluates expected-vs-observed. Retry and reporting are fully deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

# Reuse the committed AppPilot building blocks; do not fork a new framework.
# Support both `python -m src.flows.deeplink` (package-relative) and running
# with src on sys.path (top-level, e.g. via the compat shim), unchanged.
try:
    from ..apppilot.android import (  # noqa: E402
        APP_ID,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from ..apppilot.agent import _load_dotenv  # noqa: E402
    from ..apppilot.models import UIObservation  # noqa: E402
except ImportError:
    from apppilot.android import (  # noqa: E402
        APP_ID,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from apppilot.agent import _load_dotenv  # noqa: E402
    from apppilot.models import UIObservation  # noqa: E402

# Absolute deterministic bounds for the deeplink suite. These are unrelated to
# the agent's own action/stuck limits; a deeplink attempt is a single launch +
# observe + evaluate, not an action loop.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_WAIT_SECONDS = 2.0
# Time to let the app settle after a deeplink launch before observing. Injected
# via the same sleep hook so tests can make it a no-op.
DEFAULT_SETTLE_SECONDS = 3.0


# --------------------------------------------------------------------------- #
# Test cases (Excel is the source of truth; keep it simple: 4 columns)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeeplinkTestCase:
    """One row of the Excel. Exactly the four provided columns, nothing more."""

    test_id: str
    deep_link: str
    user_type: str
    expected_result: str


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# Column layout is positional (A..D), matching the intentionally simple Excel.
_COLUMNS = {"A": "test_id", "B": "deep_link", "C": "user_type", "D": "expected_result"}
_HEADER_TOKENS = {"test id", "testid", "test case", "test case id", "id", "tc"}


def _column_letter(cell_ref: str) -> str:
    return "".join(ch for ch in cell_ref if ch.isalpha())


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    strings: list[str] = []
    for si in root.findall(f"{_SHEET_NS}si"):
        strings.append("".join(t.text or "" for t in si.iter(f"{_SHEET_NS}t")))
    return strings


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        node = cell.find(f"{_SHEET_NS}is")
        return "".join(t.text or "" for t in node.iter(f"{_SHEET_NS}t")) if node is not None else ""
    value = cell.find(f"{_SHEET_NS}v")
    text = value.text if value is not None else ""
    if text is None:
        return ""
    if cell_type == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return ""
    return text


def _row_values(row: ET.Element, shared: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for cell in row.findall(f"{_SHEET_NS}c"):
        letter = _column_letter(cell.get("r", ""))
        if letter in _COLUMNS:
            values[_COLUMNS[letter]] = _cell_value(cell, shared).strip()
    return values


def _looks_like_header(values: dict[str, str]) -> bool:
    # The attached Excel has no header row; a header (if present) is detected by
    # a non-deeplink "Deep Link" cell or a recognizable Test ID label.
    test_id = values.get("test_id", "").casefold()
    deep_link = values.get("deep_link", "")
    if test_id in _HEADER_TOKENS:
        return True
    return bool(deep_link) and "://" not in deep_link


def load_deeplink_cases(path: str | Path) -> list[DeeplinkTestCase]:
    """Load deeplink test cases from the Excel workbook (stdlib only).

    Columns are positional: A=Test ID, B=Deep Link, C=User Type, D=Expected
    Result. A leading header row, if present, is skipped. Every data row must
    provide a Test ID, a Deep Link and an Expected Result.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        shared = _read_shared_strings(archive)
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    cases: list[DeeplinkTestCase] = []
    header_skipped = False
    for row in sheet.iter(f"{_SHEET_NS}row"):
        values = _row_values(row, shared)
        if not any(values.values()):
            continue
        if not header_skipped and not cases and _looks_like_header(values):
            header_skipped = True
            continue
        test_id = values.get("test_id", "")
        deep_link = values.get("deep_link", "")
        expected = values.get("expected_result", "")
        missing = [
            name
            for name, present in (
                ("Test ID", test_id),
                ("Deep Link", deep_link),
                ("Expected Result", expected),
            )
            if not present
        ]
        if missing:
            raise ValueError(
                f"Deeplink test row is missing required column(s): {', '.join(missing)}"
            )
        cases.append(
            DeeplinkTestCase(
                test_id=test_id,
                deep_link=deep_link,
                user_type=values.get("user_type", ""),
                expected_result=expected,
            )
        )
    if not cases:
        raise ValueError(f"No deeplink test cases found in {path}")
    return cases


# --------------------------------------------------------------------------- #
# AI expectation judge (semantic expected-vs-observed evaluation)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExpectationVerdict:
    matched: bool
    reason: str


class ExpectationJudge(Protocol):
    def evaluate(
        self, expected_result: str, observation: UIObservation
    ) -> ExpectationVerdict:
        ...


class LLMExpectationJudge:
    """Model-backed semantic judge: does the observed UI satisfy Expected Result?

    Uses the same OpenAI-compatible configuration as the agent's decision model
    (``APPPILOT_MODEL``, ``APPPILOT_MODEL_API_KEY``, ``APPPILOT_MODEL_BASE_URL``)
    so no provider/model/credential is hardcoded. It is given ONLY the natural
    language Expected Result and the redacted observed UI; it decides match or
    mismatch. It never selects or alters a deeplink.
    """

    _SYSTEM_PROMPT = (
        "You are AppPilot's deeplink result judge. You are given an EXPECTED "
        "RESULT written in natural language (for example \"Chat screen\", \"Chat "
        "screen with prompt\", \"Researcher screen with prompt\", or an expected "
        "error/failure state) and the CURRENT observed Android UI after a deep "
        "link was launched.\n"
        "\n"
        "Your only job is to decide whether the observed UI SEMANTICALLY "
        "satisfies the expected result. Judge by meaning, not by exact wording or "
        "specific selectors. Do NOT rely on any single hardcoded label; reason "
        "about what the screen is and whether it is the expected state.\n"
        "\n"
        "IMPORTANT:\n"
        "- The test passes when the OBSERVED state matches the EXPECTED state - "
        "not when the deeplink 'succeeded'. If the expected result describes an "
        "error or failure state and that error/failure is what is observed, that "
        "is a MATCH (pass).\n"
        "- 'with prompt' means a prompt/text is present in the composer or input; "
        "its absence when expected is a mismatch, and vice versa.\n"
        "- If the observed UI is ambiguous, incidental (a transient/loading or "
        "unrelated interruption), or clearly a different screen than expected, it "
        "is NOT a match.\n"
        "\n"
        "RESPOND with strict JSON only, no prose outside it:\n"
        '{"match": <true|false>, "reason": <string>}'
    )

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        transport: Callable[[dict], dict] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport or self._http_transport
        self._timeout = timeout

    @classmethod
    def from_env(cls, env: dict | None = None) -> "LLMExpectationJudge | None":
        env = os.environ if env is None else env
        api_key = env.get("APPPILOT_MODEL_API_KEY")
        model = env.get("APPPILOT_MODEL")
        if not api_key or not model:
            return None
        base_url = env.get("APPPILOT_MODEL_BASE_URL") or "https://api.openai.com/v1"
        return cls(model=model, api_key=api_key, base_url=base_url)

    def evaluate(
        self, expected_result: str, observation: UIObservation
    ) -> ExpectationVerdict:
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": self._render(expected_result, observation)},
            ],
            "response_format": {"type": "json_object"},
        }
        response = self._transport(payload)
        try:
            content = response["choices"][0]["message"]["content"]
            decoded = json.loads(content)
        except (KeyError, IndexError, ValueError, TypeError) as error:
            return ExpectationVerdict(
                matched=False, reason=f"Judge response could not be parsed: {error}"
            )
        matched = bool(decoded.get("match"))
        reason = str(decoded.get("reason") or "").strip() or "(no reason given)"
        return ExpectationVerdict(matched=matched, reason=reason)

    @staticmethod
    def _render(expected_result: str, observation: UIObservation) -> str:
        # observation.describe() already redacts credential fields, so no secret
        # can reach the judge prompt.
        return (
            f"EXPECTED RESULT: {expected_result}\n"
            "CURRENT UI ELEMENTS:\n"
            f"{observation.describe(limit=40)}\n"
            'Respond with JSON: {"match": true|false, "reason": "..."}'
        )

    def _http_transport(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as error:
            raise RuntimeError(f"Judge request failed: {error}") from error


# --------------------------------------------------------------------------- #
# Warm-up (first-install preparation) - runs at most once, never on retries
# --------------------------------------------------------------------------- #
class WarmUp(Protocol):
    def __call__(self) -> None:
        ...


class MaestroWarmUp:
    """Default first-install warm-up: launch the app a few times so its
    feature/config gates and initialization are fetched. Deterministic; runs
    once before the suite and is NOT repeated for deeplink retries.
    """

    def __init__(
        self,
        executor: MaestroExecutor,
        launches: int = 3,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._executor = executor
        self._launches = max(1, launches)
        self._settle_seconds = settle_seconds
        self._sleep = sleep

    def __call__(self) -> None:
        for _ in range(self._launches):
            self._executor.launch_app()
            if self._settle_seconds:
                self._sleep(self._settle_seconds)
            self._executor.stop_app()


# --------------------------------------------------------------------------- #
# Results and reporting (deterministic)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AttemptResult:
    attempt: int
    matched: bool
    reason: str


@dataclass
class TestCaseResult:
    case: DeeplinkTestCase
    attempts: list[AttemptResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return any(attempt.matched for attempt in self.attempts)

    @property
    def passing_attempt(self) -> int | None:
        for attempt in self.attempts:
            if attempt.matched:
                return attempt.attempt
        return None


@dataclass
class SuiteReport:
    results: list[TestCaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def format(self) -> str:
        lines = ["DEEPLINK TEST REPORT", ""]
        for result in self.results:
            status = "PASS" if result.passed else "FAIL"
            attempt_no = result.passing_attempt or len(result.attempts)
            lines.append(
                f"{result.case.test_id}  {status}  Attempt {attempt_no}"
            )
            lines.append(f"    Expected: {result.case.expected_result}")
            for attempt in result.attempts:
                mark = "match" if attempt.matched else "mismatch"
                lines.append(
                    f"    Attempt {attempt.attempt}: {mark} - {attempt.reason}"
                )
            lines.append("")
        lines.append(f"Total: {self.total}")
        lines.append(f"Passed: {self.passed}")
        lines.append(f"Failed: {self.failed}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The runner (deterministic orchestration; AI only judges)
# --------------------------------------------------------------------------- #
class DeeplinkTestRunner:
    def __init__(
        self,
        observer: MaestroHierarchyObserver,
        executor: MaestroExecutor,
        judge: ExpectationJudge,
        warm_up: WarmUp | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    ) -> None:
        self._observer = observer
        self._executor = executor
        self._judge = judge
        self._warm_up = warm_up
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._retry_wait_seconds = retry_wait_seconds
        self._settle_seconds = settle_seconds

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        # First-install warm-up happens once for the whole suite, if provided,
        # and is deliberately never invoked again during per-attempt retries.
        if self._warm_up is not None:
            self._warm_up()

        report = SuiteReport()
        for case in cases:
            report.results.append(self._run_case(case))
        return report

    def _run_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        result = TestCaseResult(case=case)
        for attempt in range(1, self._max_attempts + 1):
            if attempt > 1:
                # Retry recipe: kill app -> wait 2s -> execute same deeplink.
                self._executor.stop_app()
                self._sleep(self._retry_wait_seconds)

            self._executor.open_link(case.deep_link)
            if self._settle_seconds:
                self._sleep(self._settle_seconds)

            observation = self._observer.observe()
            verdict = self._judge.evaluate(case.expected_result, observation)
            result.attempts.append(
                AttemptResult(
                    attempt=attempt, matched=verdict.matched, reason=verdict.reason
                )
            )
            if verdict.matched:
                break
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default_excel_path() -> Path:
    return Path(__file__).resolve().parents[2] / "testcases" / "deeplinks" / (
        "deeplink_tests.xlsx"
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the data-driven AppPilot deeplink test suite."
    )
    parser.add_argument(
        "--excel",
        default=str(_default_excel_path()),
        help="Path to the deeplink test-case Excel (default: bundled workbook).",
    )
    parser.add_argument("--device", default="emulator-5554")
    parser.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
        help="Maximum attempts per deeplink test case (default 3).",
    )
    parser.add_argument(
        "--no-warm-up", action="store_true",
        help="Skip the one-time first-install warm-up before the suite.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv()
    args = _parse_args(argv)
    if args.max_attempts < 1:
        print("ERROR: --max-attempts must be at least 1", file=sys.stderr)
        return 2

    try:
        cases = load_deeplink_cases(args.excel)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"ERROR: could not load deeplink test cases: {error}", file=sys.stderr)
        return 2

    judge = LLMExpectationJudge.from_env()
    if judge is None:
        print(
            "ERROR: no evaluation model configured. Set APPPILOT_MODEL and "
            "APPPILOT_MODEL_API_KEY (and optionally APPPILOT_MODEL_BASE_URL).",
            file=sys.stderr,
        )
        return 2

    executor = MaestroExecutor(APP_ID, args.device)
    observer = MaestroHierarchyObserver(args.device)
    warm_up = None if args.no_warm_up else MaestroWarmUp(executor)
    runner = DeeplinkTestRunner(
        observer=observer,
        executor=executor,
        judge=judge,
        warm_up=warm_up,
        max_attempts=args.max_attempts,
    )
    report = runner.run(cases)
    print(report.format())
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
