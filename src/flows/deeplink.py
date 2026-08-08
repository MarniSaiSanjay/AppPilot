"""Data-driven deeplink test runner for AppPilot.

This reuses the existing AppPilot abstractions - the same Maestro executor,
Maestro UI observer, the shared generic login flow, and the same
OpenAI-compatible model configuration - rather than introducing a second agent
framework. Its shape is deliberately narrow:

    load test cases (Excel)
        -> for each case: launch the EXACT deeplink (deterministic, Maestro)
            -> observe the resulting Android UI (deterministic, Maestro)
                -> AI judges whether the observed UI satisfies the natural
                   language Expected Result (semantic, model)
                    -> PASS, or kill + wait + retry (deterministic)

The deeplink and the Expected Result both come from the Excel and are used
verbatim. The model never invents, modifies, or chooses a deeplink; it only
evaluates expected-vs-observed. Retry and reporting are fully deterministic.

INSTALLED vs UNINSTALLED (deterministic, from the Excel INSTALLED column):

  * INSTALLED=TRUE cases run as a batch: ensure the app is signed in via the
    SHARED login flow (AI-driven; a no-op if already signed in), then run the
    installed-environment warm-up exactly ONCE for the whole batch, then run
    each case. Per-case retry is the standard kill -> wait 2s -> reopen recipe.
    The warm-up is never repeated per case or per retry.

  * INSTALLED=FALSE cases test the genuine FIRST OPEN AFTER INSTALL: the app is
    uninstalled (clearing data is not enough), the exact deeplink is triggered
    so Android routes to the Play Store, the app is installed and opened, then
    the SAME shared login flow runs. No installed warm-up is performed. To keep
    each retry a true first-install (never a warmed-up/installed scenario), the
    fresh/uninstalled state is re-established on EVERY attempt instead of the
    kill -> wait -> reopen recipe.

LIMITATION - Play Store install/open is best-effort: it taps the Play Store's
visible "Install"/"Open" buttons by text via Maestro (a deterministic selector,
NOT coordinate taps and NOT AI-driven), so it depends on the Play Store's
current button labels and cannot be guaranteed across store versions. It is
injected behind an interface so it can be replaced; no LLM is ever used to
decide how to install the app.
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

# The SHARED login capability (same generic AppPilotAgent + Brain) - reused, not
# duplicated. Sibling import works whether this module is loaded as
# ``src.flows.deeplink`` or top-level ``flows.deeplink`` (via the compat shim).
from .login import DEFAULT_GUIDANCE, PROTOTYPE_GOAL, build_login_agent  # noqa: E402

# Absolute deterministic bounds for the deeplink suite. These are unrelated to
# the agent's own action/stuck limits; a deeplink attempt is a single launch +
# observe + evaluate, not an action loop.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_WAIT_SECONDS = 2.0
# Time to let the app settle after a deeplink launch before observing. Injected
# via the same sleep hook so tests can make it a no-op.
DEFAULT_SETTLE_SECONDS = 3.0
# Time to allow a Play Store install to complete before tapping Open. Best-effort
# and injectable so tests make it a no-op.
DEFAULT_INSTALL_WAIT_SECONDS = 90.0


# --------------------------------------------------------------------------- #
# Test cases (Excel is the source of truth; keep it simple: 4 columns)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeeplinkTestCase:
    """One row of the Excel: the four core columns plus the deterministic
    INSTALLED scenario selector (optional, defaults to installed=True)."""

    test_id: str
    deep_link: str
    user_type: str
    expected_result: str
    # Deterministic scenario selector from the Excel INSTALLED column. Absent or
    # blank preserves the legacy contract (installed=True). The model NEVER
    # decides this - it comes straight from the workbook.
    installed: bool = True


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# Positional fallback used only when no header row can be recognised. The parser
# is not strict about exact layout: when a header row IS present, columns are
# mapped by header name (see _map_header) so extra/renamed/reordered columns and
# leading title rows are all handled generically.
_COLUMNS = {
    "A": "test_id",
    "B": "deep_link",
    "C": "user_type",
    "D": "expected_result",
    "E": "installed",
}
# Header-name synonyms per logical field (casefolded, whitespace-collapsed).
# Matching is substring-based and tolerant so real-world headers like
# "Launch URL", "Expected Screen" or "License" map without exact-string coupling.
_FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "test_id": ("test case id", "test id", "testid", "test case", "testcase", "case id"),
    "deep_link": ("launch url", "deep link", "deeplink", "launch link", "url", "link", "launch"),
    "user_type": ("license", "licence", "account", "user type", "user", "persona", "plan", "subscription"),
    "expected_result": ("expected screen", "expected result", "expected", "result", "screen"),
    "installed": ("installed", "install state", "app installed", "fresh", "uninstalled"),
}
# Only explicit negatives mean a fresh/uninstalled first-open scenario; anything
# else (including blank/unknown, "yes", "true") means the app is installed.
_INSTALLED_FALSE = {"false", "f", "no", "n", "0", "uninstalled", "not installed", "fresh"}
# Deterministic signal (not model-driven) that a deeplink targets the genuine
# first-open-after-install experience: the URL asks the app store to open on
# load, which only makes sense when the app is not yet installed.
_APP_STORE_ON_LOAD = "openappstoreonload=true"


def _parse_installed(raw: str) -> bool:
    return (raw or "").strip().casefold() not in _INSTALLED_FALSE


def _derive_installed(deep_link: str) -> bool:
    """Deterministically infer the INSTALLED scenario from the deeplink when the
    workbook has no explicit INSTALLED column. A deeplink that routes to the app
    store on load is a first-open-after-install (uninstalled) case."""
    return _APP_STORE_ON_LOAD not in (deep_link or "").casefold()


def _normalize_label(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _match_field(label: str) -> str | None:
    """Map a header cell label to a logical field, preferring the most specific
    (longest) synonym match so e.g. 'Expected Screen' maps to expected_result
    rather than the shorter 'screen'."""
    norm = _normalize_label(label)
    if not norm:
        return None
    best_field: str | None = None
    best_len = 0
    for field, synonyms in _FIELD_SYNONYMS.items():
        for synonym in synonyms:
            if synonym == norm or synonym in norm:
                if len(synonym) > best_len:
                    best_field, best_len = field, len(synonym)
    return best_field


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


def _row_cells(row: ET.Element, shared: list[str]) -> dict[str, str]:
    """Read ALL populated cells of a row as {column_letter: stripped_value}."""
    cells: dict[str, str] = {}
    for cell in row.findall(f"{_SHEET_NS}c"):
        letter = _column_letter(cell.get("r", ""))
        if letter:
            cells[letter] = _cell_value(cell, shared).strip()
    return cells


def _looks_like_data(cells: dict[str, str]) -> bool:
    # A data row carries an actual deeplink (has a URL scheme); header/title rows
    # do not. This cleanly separates the first data row from any leading
    # title/header rows without depending on exact positions.
    return any("://" in value for value in cells.values())


def _map_header(cells: dict[str, str]) -> dict[str, str]:
    """Map header cells to logical fields: {column_letter: field}. Each field is
    assigned at most once (first, most-specific match wins)."""
    mapping: dict[str, str] = {}
    assigned: set[str] = set()
    # Resolve per-cell best field, then assign in column order avoiding clashes.
    scored = [
        (letter, _match_field(label)) for letter, label in cells.items()
    ]
    for letter, field in sorted(scored, key=lambda item: item[0]):
        if field and field not in assigned:
            mapping[letter] = field
            assigned.add(field)
    return mapping


def _is_usable_header(mapping: dict[str, str]) -> bool:
    fields = set(mapping.values())
    return "deep_link" in fields and "expected_result" in fields


def load_deeplink_cases(path: str | Path) -> list[DeeplinkTestCase]:
    """Load deeplink test cases from the Excel workbook (stdlib only).

    The parser is intentionally tolerant of layout. It recognises a header row by
    name (e.g. "Launch URL", "Expected Screen", "License") and maps columns
    accordingly, so leading title rows, renamed/reordered columns and optional
    extra columns are all handled. When no header is present it falls back to the
    positional layout A=Test ID, B=Deep Link, C=User Type, D=Expected Result.

    Every data row must provide a Test ID, a Deep Link and an Expected Result.
    The INSTALLED scenario is taken from an explicit INSTALLED column when
    present, otherwise derived deterministically from the deeplink.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        shared = _read_shared_strings(archive)
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows = [
        cells
        for cells in (_row_cells(row, shared) for row in sheet.iter(f"{_SHEET_NS}row"))
        if any(cells.values())
    ]

    # Recognise a header among the leading non-data rows (title rows are simply
    # ignored: they map too few fields to be a usable header).
    column_map = _COLUMNS
    data_start = 0
    for index, cells in enumerate(rows):
        if _looks_like_data(cells):
            data_start = index
            break
        candidate = _map_header(cells)
        if _is_usable_header(candidate):
            column_map = candidate
    else:
        # No data row found (only title/header rows, or empty sheet).
        data_start = len(rows)

    cases: list[DeeplinkTestCase] = []
    for cells in rows[data_start:]:
        values = {
            field: cells.get(letter, "") for letter, field in column_map.items()
        }
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
        installed_raw = values.get("installed", "")
        installed = (
            _parse_installed(installed_raw)
            if installed_raw
            else _derive_installed(deep_link)
        )
        cases.append(
            DeeplinkTestCase(
                test_id=test_id,
                deep_link=deep_link,
                user_type=values.get("user_type", ""),
                expected_result=expected,
                installed=installed,
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
# Shared login capability + fresh-install (Play Store) capability
# --------------------------------------------------------------------------- #
class LoginCapability(Protocol):
    def ensure_ready(self) -> None:
        ...


class SharedLoginFlow:
    """Ensures the app is signed-in/ready via the SHARED generic login flow
    (flows.login -> AppPilotAgent + Brain). No login UI is hardcoded: the agent's
    goal evaluator reports success immediately when already signed in, otherwise
    the model drives each onboarding action. The SAME instance is reused by both
    the installed and uninstalled scenarios (DRY)."""

    def __init__(self, agent) -> None:
        self._agent = agent

    def ensure_ready(self) -> None:
        self._agent.run(PROTOTYPE_GOAL, DEFAULT_GUIDANCE)


class AppInstaller(Protocol):
    def ensure_absent(self) -> None:
        ...

    def install_and_open(self) -> None:
        ...


class PlayStoreInstaller:
    """Best-effort, deterministic Play Store install + open (see module
    LIMITATION). ``ensure_absent`` genuinely uninstalls the APK (adb) so the
    next deeplink is a real first-open; ``install_and_open`` taps the Play
    Store's visible "Install" then "Open" buttons by text via Maestro. No
    coordinate taps and no LLM are involved."""

    def __init__(
        self,
        executor: MaestroExecutor,
        *,
        sleep: Callable[[float], None] = time.sleep,
        install_wait_seconds: float = DEFAULT_INSTALL_WAIT_SECONDS,
        install_text: str = "Install",
        open_text: str = "Open",
    ) -> None:
        self._executor = executor
        self._sleep = sleep
        self._install_wait_seconds = install_wait_seconds
        self._install_text = install_text
        self._open_text = open_text

    def ensure_absent(self) -> None:
        self._executor.ensure_uninstalled()

    def install_and_open(self) -> None:
        self._executor.tap_text(self._install_text)
        if self._install_wait_seconds:
            self._sleep(self._install_wait_seconds)
        self._executor.tap_text(self._open_text)


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
        login_flow: LoginCapability | None = None,
        installer: AppInstaller | None = None,
    ) -> None:
        self._observer = observer
        self._executor = executor
        self._judge = judge
        self._warm_up = warm_up
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._retry_wait_seconds = retry_wait_seconds
        self._settle_seconds = settle_seconds
        self._login_flow = login_flow
        self._installer = installer

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        # Delegate the top-level lifecycle to the explicit orchestrator, which
        # composes this runner's per-case execution. Kept as a convenience entry
        # point so existing callers/tests that hold a runner still work.
        return DeeplinkSuiteOrchestrator(self).run(cases)

    def ensure_logged_in(self) -> None:
        # Login ONLY if needed, via the SHARED login capability. The underlying
        # AppPilotAgent + SignedInCopilotGoalEvaluator report ready and take no
        # actions when already signed in; otherwise the Brain drives onboarding.
        if self._login_flow is not None:
            self._login_flow.ensure_ready()

    def run_warm_up(self) -> None:
        # The installed warm-up (launch -> wait -> stop, x3). Invoked once per
        # installed batch by the orchestrator - never per case, never on retry.
        if self._warm_up is not None:
            self._warm_up()

    def run_installed_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run a single INSTALLED case (kill -> wait 2s -> reopen retry)."""
        return self._run_case(case)

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run a single UNINSTALLED first-open case (fresh state every attempt)."""
        return self._run_uninstalled_case(case)

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

    def _run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        # First-open-after-install scenario. There is NO installed warm-up. To
        # keep every attempt a genuine first open, the fresh/uninstalled state is
        # re-established on each attempt (uninstalling if present) rather than the
        # kill -> wait -> reopen recipe, which would leave the app installed and
        # silently degrade a retry into an installed scenario.
        result = TestCaseResult(case=case)
        for attempt in range(1, self._max_attempts + 1):
            if self._installer is not None:
                self._installer.ensure_absent()

            # The EXACT Excel deeplink triggers the flow; with the app absent
            # Android routes to the Play Store.
            self._executor.open_link(case.deep_link)
            if self._installer is not None:
                self._installer.install_and_open()

            # Same SHARED login/onboarding flow as the installed scenario.
            if self._login_flow is not None:
                self._login_flow.ensure_ready()

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
# Suite orchestrator (explicit top-level lifecycle; composes the runner)
# --------------------------------------------------------------------------- #
class DeeplinkSuiteOrchestrator:
    """Makes the deeplink suite lifecycle explicit and readable.

    It owns only the top-level flow - splitting cases by the deterministic
    INSTALLED value, preparing the installed batch (login-if-needed + one-time
    warm-up), and driving each case - while COMPOSING the existing
    DeeplinkTestRunner for individual case execution, semantic judging, retry
    behavior, and reporting. Nothing here duplicates the runner, the shared
    login capability, the judge, the Play Store installer, or Maestro/Android
    behavior.

        run()
            -> INSTALLED batch: prepare_installed_batch() then run each case
            -> UNINSTALLED cases: run each first-open case (no warm-up)
            -> final SuiteReport
    """

    def __init__(self, runner: DeeplinkTestRunner) -> None:
        self._runner = runner

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        # INSTALLED is deterministic (from Excel); installed cases run as one
        # batch (single login-if-needed + single warm-up), then each uninstalled
        # first-open case runs independently.
        installed = [case for case in cases if case.installed]
        uninstalled = [case for case in cases if not case.installed]

        report = SuiteReport()
        if installed:
            self.run_installed_batch(installed, report)
        for case in uninstalled:
            report.results.append(self.run_uninstalled_case(case))
        return report

    def prepare_installed_batch(self) -> None:
        # 1) Ensure signed in via the SHARED login capability (a no-op when
        #    already signed in). 2) Run the installed warm-up EXACTLY ONCE for the
        #    whole batch - never per case and never during a retry.
        self._runner.ensure_logged_in()
        self._runner.run_warm_up()

    def run_installed_batch(
        self, cases: Sequence[DeeplinkTestCase], report: SuiteReport
    ) -> None:
        self.prepare_installed_batch()
        for case in cases:
            report.results.append(self._runner.run_installed_case(case))

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        # First-open-after-install: NO warm-up. The runner re-establishes the
        # genuine fresh/uninstalled state on every attempt.
        return self._runner.run_uninstalled_case(case)


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

    # Shared login capability: the SAME generic AppPilotAgent + Brain used by the
    # standalone login flow, reused (DRY) by both installed and uninstalled cases.
    # The device's executor/observer are reused so no infrastructure is duplicated.
    login_agent = build_login_agent(args.device, executor=executor, observer=observer)
    login_flow: LoginCapability = SharedLoginFlow(login_agent)

    installer = PlayStoreInstaller(executor)
    runner = DeeplinkTestRunner(
        observer=observer,
        executor=executor,
        judge=judge,
        warm_up=warm_up,
        max_attempts=args.max_attempts,
        login_flow=login_flow,
        installer=installer,
    )
    # The orchestrator owns the explicit top-level lifecycle and composes the
    # runner for per-case execution, judging, retry and reporting.
    report = DeeplinkSuiteOrchestrator(runner).run(cases)
    print(report.format())
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
