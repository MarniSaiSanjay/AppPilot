"""Data-driven deeplink test runner for AppPilot.

Reuses the existing AppPilot abstractions (Maestro executor/observer, the shared
login flow, the OpenAI-compatible model config) rather than a second framework:

    load test cases (Excel)
        -> for each case: launch the EXACT deeplink (deterministic, Maestro)
            -> observe the resulting Android UI (deterministic, Maestro)
                -> AI judges observed UI vs the Expected Result (semantic)
                    -> PASS, or kill + wait + retry (deterministic)

Deeplink and Expected Result come from the Excel verbatim; the model only judges
expected-vs-observed. Retry and reporting are fully deterministic.

INSTALLED vs UNINSTALLED (from the Excel INSTALLED column):

  * INSTALLED=TRUE: run as a batch - sign in via the shared login flow (no-op if
    already signed in), run the installed warm-up exactly ONCE for the batch,
    then each case. Per-case retry is kill -> wait 2s -> reopen (no re-warm-up).

  * INSTALLED=FALSE: test the genuine FIRST OPEN AFTER INSTALL - uninstall the
    app, trigger the deeplink (Android routes to the store window), install the
    local APK via adb, launch directly via a deterministic adb command (monkey
    LAUNCHER intent, NOT the store "Open" button, NOT re-firing the deeplink;
    the app recovers the deferred deeplink itself). The app is confirmed
    FOREGROUND via adb before the shared login flow runs. No installed warm-up.
    Every retry re-establishes the fresh/uninstalled state (never a warmed-up
    scenario).

INSTALL is always from the locally built APK via ``adb install`` (never the Play
Store), behind a replaceable interface; no LLM decides how to install or launch.
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
from typing import Callable, Protocol, Sequence

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
    from ..apppilot import officemobile_build  # noqa: E402
except ImportError:
    from apppilot.android import (  # noqa: E402
        APP_ID,
        MaestroExecutor,
        MaestroHierarchyObserver,
    )
    from apppilot.agent import _load_dotenv  # noqa: E402
    from apppilot.models import UIObservation  # noqa: E402
    from apppilot import officemobile_build  # noqa: E402

# The SHARED login capability (same generic AppPilotAgent + Brain) - reused, not
# duplicated. Sibling import works whether this module is loaded as
# ``src.flows.deeplink`` or top-level ``flows.deeplink`` (via the compat shim).
from .login import DEFAULT_GUIDANCE, PROTOTYPE_GOAL, build_login_agent  # noqa: E402

# Deterministic bounds for the deeplink suite (separate from the agent's
# action/stuck limits). A failed test is retried once (1 attempt + 1 retry).
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_WAIT_SECONDS = 2.0
# Settle time after a deeplink launch before the first verification observation.
# Injected via the sleep hook so tests can no-op it.
DEFAULT_SETTLE_SECONDS = 3.0
# Bounded verification polling: observe -> judge repeatedly, PASS on first match,
# mismatch only after the window elapses. Same for installed AND uninstalled.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 15.0
DEFAULT_VERIFY_POLL_INTERVAL_SECONDS = 2.0
# Bounded wait for the app to become foreground after an adb launch (a returning
# launch command is not proof); confirmed via the deterministic adb foreground check.
DEFAULT_FOREGROUND_TIMEOUT_SECONDS = 30.0
DEFAULT_FOREGROUND_POLL_SECONDS = 1.0


def _trace(message: str) -> None:
    """Emit one execution-trace line to stdout.

    This is deliberately a dumb printer, NOT a narration helper: every call site
    sits immediately next to a REAL operation (a launch, a stop, an open_link, an
    ensure_absent, an install, a login, a judge, ...) so the terminal output is a
    faithful chronological record of what actually executed. A success line is
    only emitted after the underlying call returns, so if an operation raises the
    corresponding "done" line is never printed. Secrets and full deeplink URLs
    are never passed in here.
    """
    print(message)


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
# Positional fallback used only when no header row is recognised; otherwise
# columns are mapped by header name (see _map_header).
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
    for logical_field, synonyms in _FIELD_SYNONYMS.items():
        for synonym in synonyms:
            if synonym == norm or synonym in norm:
                if len(synonym) > best_len:
                    best_field, best_len = logical_field, len(synonym)
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
    for letter, logical_field in sorted(scored, key=lambda item: item[0]):
        if logical_field and logical_field not in assigned:
            mapping[letter] = logical_field
            assigned.add(logical_field)
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
            logical_field: cells.get(letter, "")
            for letter, logical_field in column_map.items()
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
        "screen with prompt\", \"Chat screen with prompt \\\"<specific text>\\\"\", "
        "\"Researcher screen with prompt\", or an expected error/failure state) "
        "and the CURRENT observed Android UI after a deep link was launched.\n"
        "\n"
        "Decide whether the observed UI SEMANTICALLY satisfies the expected "
        "result. Judge the screen TYPE by meaning, not by exact wording or "
        "specific selectors, and do not rely on any single hardcoded label.\n"
        "\n"
        "RULES:\n"
        "- Match on the OBSERVED state, not on whether the deeplink 'succeeded'. "
        "If the expected result describes an error/failure state and that is what "
        "is observed, that is a MATCH.\n"
        "- 'with prompt' with NO specific text: a non-empty prompt/text must be "
        "present in the composer/input; its expected presence/absence must "
        "agree.\n"
        "- If the expected result SPECIFIES OR QUOTES a particular prompt, topic, "
        "or content, the observed composer/input must actually contain THAT SAME "
        "prompt (same meaning/topic) - not merely some text. A generic, "
        "placeholder, suggested, or DIFFERENT prompt is a MISMATCH. Minor "
        "wording/whitespace differences are acceptable; a different topic or a "
        "different prompt is NOT.\n"
        "- An initial suggested-prompt / welcome / onboarding screen that shows a "
        "random or different suggested prompt does NOT satisfy an expected "
        "specific prompt.\n"
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
        _trace(
            f"[WARM-UP] Starting installed-app preparation: {self._launches} cycles"
        )
        for index in range(self._launches):
            cycle = index + 1
            _trace(f"[WARM-UP] Cycle {cycle}/{self._launches}: launch app")
            self._executor.launch_app()
            if self._settle_seconds:
                _trace(
                    f"[WARM-UP] Cycle {cycle}/{self._launches}: "
                    f"waiting {self._settle_seconds:g}s"
                )
                self._sleep(self._settle_seconds)
            _trace(f"[WARM-UP] Cycle {cycle}/{self._launches}: stop app")
            self._executor.stop_app()
            _trace(f"[WARM-UP] Cycle {cycle}/{self._launches} complete")
        _trace("[WARM-UP] Installed-app preparation complete")


# --------------------------------------------------------------------------- #
# Shared login capability + fresh-install (local APK via adb) capability
# --------------------------------------------------------------------------- #
class LoginCapability(Protocol):
    def ensure_ready(self) -> bool:
        """Return True iff login preparation reached success. This is the sole
        signal the caller inspects; it never implies deeplink success."""
        ...


class SharedLoginFlow:
    """Ensures the app is signed-in/ready via the SHARED generic login flow
    (flows.login -> AppPilotAgent + Brain). No login UI is hardcoded: the agent's
    goal evaluator reports success immediately when already signed in, otherwise
    the model drives each onboarding action. The SAME instance is reused by both
    the installed and uninstalled scenarios (DRY)."""

    def __init__(self, agent) -> None:
        self._agent = agent
        # Observability only: wrap the agent's goal evaluator so the [LOGIN] trace
        # reflects real verdicts. The wrapper returns each verdict verbatim, so
        # behavior is unchanged. Guard against double-wrapping if reused.
        self._tracer: _SignInTracer | None = None
        evaluator = getattr(agent, "_goal_evaluator", None)
        if evaluator is not None and not isinstance(evaluator, _SignInTracer):
            self._tracer = _SignInTracer(evaluator)
            agent._goal_evaluator = self._tracer

    def ensure_ready(self) -> bool:
        # Reset the per-run trace state so each login attempt reports its own
        # already/required/completed verdict (the login flow is reused across the
        # installed batch and every uninstalled attempt).
        if self._tracer is not None:
            self._tracer.begin_run()
        # Propagate the agent's verdict verbatim (True = login goal reached,
        # False = preparation failed) - do NOT swallow it.
        return bool(self._agent.run(PROTOTYPE_GOAL, DEFAULT_GUIDANCE))


class _SignInTracer:
    """Observability wrapper around the shared login goal evaluator.

    Emits [LOGIN] trace lines derived only from the real verdicts the underlying
    evaluator returns, passing those verdicts through unchanged:

    * the first verdict decides "Already signed in" vs "Sign-in required";
    * a later transition to signed-in is the actual completion.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._seen_first = False
        self._required = False

    def begin_run(self) -> None:
        self._seen_first = False
        self._required = False
        # Reset any per-run state the wrapped evaluator exposes. The boundary
        # evaluator is stateless (no-op here); kept as a forward-safe hook.
        inner_begin = getattr(self._inner, "begin_run", None)
        if callable(inner_begin):
            inner_begin()

    def is_reached(self, goal: str, observation: UIObservation) -> bool:
        reached = self._inner.is_reached(goal, observation)
        if not self._seen_first:
            self._seen_first = True
            if reached:
                _trace("[LOGIN] Already signed in - no login actions required")
            else:
                self._required = True
                _trace("[LOGIN] Sign-in required - starting shared login flow")
        elif reached and self._required:
            self._required = False
            _trace("[LOGIN] Authentication/onboarding boundary reached")
            _trace("[LOGIN] Login goal reached: true")
            _trace("[LOGIN] Returning control to deeplink test")
        return reached


class AppInstaller(Protocol):
    def ensure_absent(self) -> bool:
        ...

    def install_fresh(self) -> None:
        ...

    def open(self) -> None:
        ...

    def install_and_open(self) -> None:
        ...


class LocalApkInstaller:
    """Deterministic install of the locally built APK via adb, then open.

    Replaces installing from the Play Store: we ``adb install`` the local build
    directly, then launch it with a deterministic adb CLI command (monkey
    LAUNCHER intent). This is NOT the Play Store "Open" button (whose Maestro tap
    could silently no-op) and NOT re-firing the deeplink. The app recovers the
    pending (deferred) deeplink itself on this launch, so no deeplink intent is
    re-sent. After launch the app is confirmed foreground via a deterministic adb
    check before control is handed on. ``install_fresh`` is also used by the
    installed batch to put the local build on the device up front. No coordinate
    taps and no LLM are involved.
    """

    def __init__(
        self,
        executor: MaestroExecutor,
        apk_path: str,
        *,
        foreground_timeout_seconds: float = DEFAULT_FOREGROUND_TIMEOUT_SECONDS,
        foreground_poll_seconds: float = DEFAULT_FOREGROUND_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executor = executor
        self._apk_path = apk_path
        self._foreground_timeout_seconds = max(0.0, foreground_timeout_seconds)
        self._foreground_poll_seconds = max(0.0, foreground_poll_seconds)
        self._sleep = sleep
        self._monotonic = monotonic

    def ensure_absent(self) -> bool:
        return self._executor.ensure_uninstalled()

    def install_fresh(self) -> None:
        _trace(f"[INSTALL] installing local build: {self._apk_path}")
        self._executor.install_apk(self._apk_path)

    def install_and_open(self) -> None:
        self.install_fresh()
        self.open()

    def open(self) -> None:
        # Launch the already-installed build via adb (NOT the store button, NOT
        # the deeplink); return only once confirmed foreground.
        _trace("[INSTALL] launching app via adb")
        self._executor.launch_app_via_adb()
        self._wait_until_foreground()

    def _wait_until_foreground(self) -> None:
        _trace("[INSTALL] waiting for target app to become foreground")
        deadline = self._monotonic() + self._foreground_timeout_seconds
        while True:
            if self._executor.is_foreground():
                _trace("[INSTALL] target app is foreground")
                return
            if self._monotonic() >= deadline:
                raise RuntimeError(
                    "[INSTALL] target app did not become foreground within timeout"
                )
            self._sleep(self._foreground_poll_seconds)
            # The first launch can be missed while the store window is still
            # settling; re-launch via adb (best-effort) and poll again. A
            # re-launch failure is not fatal here - keep polling until foreground
            # or timeout.
            try:
                self._executor.launch_app_via_adb()
            except RuntimeError:
                pass


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
        lines = [
            "DEEPLINK TEST REPORT",
            "(Login is a precondition, reported inline per run as [LOGIN RESULT]. "
            "The PASS/FAIL below is the deeplink verification result, which is the "
            "overall test-case result.)",
            "",
        ]
        for result in self.results:
            status = "PASS" if result.passed else "FAIL"
            attempt_no = result.passing_attempt or len(result.attempts)
            lines.append(
                f"{result.case.test_id}  {status}  Attempt {attempt_no}"
            )
            lines.append(f"    Deeplink test: {status}")
            lines.append(f"    Overall test case: {status}")
            lines.append(f"    Expected: {result.case.expected_result}")
            for attempt in result.attempts:
                mark = "match" if attempt.matched else "mismatch"
                lines.append(
                    f"    Attempt {attempt.attempt}: {mark} - {attempt.reason}"
                )
            lines.append("")
        lines.append(f"Total test cases: {self.total}")
        lines.append(f"Deeplink test cases passed: {self.passed}")
        lines.append(f"Deeplink test cases failed: {self.failed}")
        return "\n".join(lines)


def _login_failed_result(case: DeeplinkTestCase) -> TestCaseResult:
    """FAIL result for a case whose login failed: one failed attempt, no deeplink
    verdict (``_verify()`` never ran). Used by the installed batch on login
    failure; the uninstalled flow reaches this via the per-attempt setup path."""
    return TestCaseResult(
        case=case,
        attempts=[
            AttemptResult(
                attempt=1,
                matched=False,
                reason="login preparation failed; deeplink verification skipped",
            )
        ],
    )


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
        verify_timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        verify_poll_interval_seconds: float = DEFAULT_VERIFY_POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
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
        self._verify_timeout_seconds = max(0.0, verify_timeout_seconds)
        self._verify_poll_interval_seconds = max(0.0, verify_poll_interval_seconds)
        self._monotonic = monotonic
        self._login_flow = login_flow
        self._installer = installer

    def run(self, cases: Sequence[DeeplinkTestCase]) -> SuiteReport:
        # Delegate the top-level lifecycle to the explicit orchestrator, which
        # composes this runner's per-case execution. Kept as a convenience entry
        # point so existing callers/tests that hold a runner still work.
        return DeeplinkSuiteOrchestrator(self).run(cases)

    def ensure_logged_in(self) -> bool:
        # Login only if needed, via the shared login capability. Returns True iff
        # login succeeded (True when no login flow is configured).
        if self._login_flow is not None:
            return self._login_flow.ensure_ready()
        return True

    def run_warm_up(self) -> None:
        # The installed warm-up (launch -> wait -> stop, x3). Invoked once per
        # installed batch by the orchestrator - never per case, never on retry.
        if self._warm_up is not None:
            self._warm_up()

    def install_local_build(self) -> None:
        # Put the freshly built local APK on the device (adb install -r). Used by
        # the installed batch so every installed case runs against the local build.
        if self._installer is not None:
            self._installer.install_fresh()

    def ensure_clean_install_state(self) -> None:
        # One-time suite-startup cleanup: guarantee the app is uninstalled so
        # every run starts from a deterministic clean state. No-op if absent or
        # no installer is configured.
        if self._installer is None:
            return
        if self._installer.ensure_absent():
            _trace("[SUITE] Removed existing app install")
        else:
            _trace("[SUITE] No existing app install to remove")

    def open_installed_app(self) -> None:
        # Launch the already-installed build to the foreground so login observes
        # the APP, not the launcher home screen. The batch only installs the APK
        # (install_fresh); the uninstalled path launches via install_and_open.
        if self._installer is not None:
            self._installer.open()

    def run_installed_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run a single INSTALLED case (kill -> wait 2s -> reopen retry)."""
        return self._run_case(case)

    def run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """Run a single UNINSTALLED first-open case (fresh state every attempt)."""
        return self._run_uninstalled_case(case)

    def _verify(self, case: DeeplinkTestCase, attempt: int):
        """Shared, bounded verification polling for a single attempt.

        Used IDENTICALLY by installed and uninstalled cases. After the deeplink
        has been executed and the app is ready to be observed, repeatedly
        observe -> judge until the expected result matches (PASS immediately) or
        the bounded verification window elapses (genuine mismatch -> caller
        retries). A deeplink destination may take several seconds to appear, so a
        first non-matching observation is NOT a failure. Uses a monotonic clock
        so the window can never be skewed by wall-clock jumps, and always makes
        at least one observe/judge call.
        """
        deadline = self._monotonic() + self._verify_timeout_seconds
        verdict = None
        while True:
            _trace(
                f"[VERIFY] {case.test_id} attempt "
                f"{attempt}/{self._max_attempts}: checking expected result"
            )
            observation = self._observer.observe()
            verdict = self._judge.evaluate(case.expected_result, observation)
            if verdict.matched:
                _trace(f"[VERIFY] {case.test_id}: expected result matched")
                return verdict
            if self._monotonic() >= deadline:
                _trace(f"[VERIFY] {case.test_id}: verification timeout reached")
                return verdict
            _trace(
                f"[VERIFY] {case.test_id}: expected result not reached; "
                f"waiting {self._verify_poll_interval_seconds:g}s"
            )
            self._sleep(self._verify_poll_interval_seconds)

    def _run_attempts(
        self,
        case: DeeplinkTestCase,
        label: str,
        prepare: Callable[[int], None],
        on_start: "Callable[[], None] | None" = None,
    ) -> TestCaseResult:
        """Generic attempt loop shared by every scenario.

        Scenarios differ ONLY in how each attempt PREPARES the app before
        verification (``prepare``); the retry loop, shared bounded verification,
        per-attempt result recording and PASS/FAIL reporting are identical. A
        preparation failure is a retryable failed attempt, never a crash, so the
        suite always continues.
        """
        result = TestCaseResult(case=case)
        _trace(f"[{label}] {case.test_id} starting")
        if on_start is not None:
            on_start()
        for attempt in range(1, self._max_attempts + 1):
            _trace(f"[{label}] {case.test_id} attempt {attempt}/{self._max_attempts}")
            try:
                prepare(attempt)
            except RuntimeError as exc:
                _trace(f"[{label}] {case.test_id} attempt setup failed: {exc}")
                result.attempts.append(
                    AttemptResult(attempt=attempt, matched=False, reason=str(exc))
                )
                continue

            if self._settle_seconds:
                self._sleep(self._settle_seconds)

            _trace(f"[{label}] {case.test_id} verifying deeplink expected result")
            verdict = self._verify(case, attempt)
            result.attempts.append(
                AttemptResult(
                    attempt=attempt, matched=verdict.matched, reason=verdict.reason
                )
            )
            if verdict.matched:
                break
            _trace(f"[{label}] {case.test_id} attempt {attempt}/{self._max_attempts}: MISMATCH")
        _trace(
            f"[{label}] {case.test_id} deeplink test case result: "
            f"{'PASS' if result.passed else 'FAIL'}"
        )
        return result

    def _run_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """INSTALLED scenario: retry recipe is kill -> wait -> reopen same deeplink.

        Process-isolated: the app is always stopped when the case finishes (PASS,
        FAIL, or error) via one finally boundary, so the next case's deeplink
        launches a fresh process. Installed-only; uninstalled is untouched.
        """
        try:
            return self._run_attempts(
                case, "INSTALLED", self._installed_prepare(case)
            )
        finally:
            _trace(f"[INSTALLED] {case.test_id} stopping app (case cleanup)")
            self._executor.stop_app()

    def _installed_prepare(self, case: DeeplinkTestCase) -> Callable[[int], None]:
        def prepare(attempt: int) -> None:
            if attempt > 1:  # retry recipe: kill -> wait -> reopen the same deeplink
                _trace(f"[INSTALLED] {case.test_id} retry: stopping app")
                self._executor.stop_app()
                _trace(
                    f"[INSTALLED] {case.test_id} retry: "
                    f"waiting {self._retry_wait_seconds:g}s"
                )
                self._sleep(self._retry_wait_seconds)
                _trace(f"[INSTALLED] {case.test_id} retry: reopening same deeplink")
            else:
                _trace(f"[INSTALLED] {case.test_id} opening deeplink")
            self._executor.open_link(case.deep_link)

        return prepare

    def _run_uninstalled_case(self, case: DeeplinkTestCase) -> TestCaseResult:
        """UNINSTALLED first-open scenario: NO warm-up. Every attempt re-establishes
        genuine fresh state (uninstall -> deeplink -> install/open -> shared login),
        so a retry can never silently degrade into an installed run."""
        return self._run_attempts(
            case,
            "UNINSTALLED",
            self._uninstalled_prepare(case),
            on_start=lambda: _trace(
                f"[UNINSTALLED] {case.test_id} first-open flow - warm-up not applicable"
            ),
        )

    def _uninstalled_prepare(self, case: DeeplinkTestCase) -> Callable[[int], None]:
        def prepare(attempt: int) -> None:
            if attempt > 1:  # every retry rebuilds genuine fresh state
                _trace(
                    f"[UNINSTALLED] {case.test_id} retry: recreating fresh-install state"
                )
            if self._installer is not None:
                _trace(f"[UNINSTALLED] {case.test_id} ensuring app is uninstalled")
                self._installer.ensure_absent()
                _trace(f"[UNINSTALLED] {case.test_id} app is uninstalled")
            # 1) The EXACT deeplink routes to the store window while absent.
            _trace(f"[UNINSTALLED] {case.test_id} opening deeplink")
            self._executor.open_link(case.deep_link)
            if self._installer is not None:
                # 2) Install the local build via adb, then 3) launch it with a
                # deterministic adb CLI command (NOT the store's "Open" button
                # and NOT re-firing the deeplink). "app opened" is only emitted
                # after the app is confirmed foreground.
                _trace(f"[INSTALL] {case.test_id} installing local build and launching app")
                self._installer.install_and_open()
                _trace(f"[INSTALL] {case.test_id} app opened")
            if self._login_flow is not None:  # SAME shared login as the installed path
                _trace(f"[UNINSTALLED] {case.test_id} ensuring login")
                # On login failure, raise into the per-attempt setup-failure path
                # (failed attempt -> skip _verify() -> retry fresh / else FAIL)
                # instead of reporting ready.
                if not self._login_flow.ensure_ready():
                    _trace(f"[UNINSTALLED] {case.test_id} login failed")
                    _trace(
                        f"[UNINSTALLED] {case.test_id} skipping deeplink verification"
                    )
                    raise RuntimeError("login preparation failed")
                _trace(f"[UNINSTALLED] {case.test_id} login ready")

        return prepare


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
    login capability, the judge, the app installer, or Maestro/Android
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

        _trace("[SUITE] Starting deeplink test suite")
        _trace(f"[SUITE] Loaded {len(cases)} test cases")
        _trace(f"[SUITE] Installed cases: {len(installed)}")
        _trace(f"[SUITE] Uninstalled cases: {len(uninstalled)}")

        # One-time clean state: uninstall the app once at suite startup so every
        # run begins deterministically. Existing per-case semantics are unchanged.
        self._runner.ensure_clean_install_state()

        report = SuiteReport()
        if installed:
            self.run_installed_batch(installed, report)
        for case in uninstalled:
            report.results.append(self.run_uninstalled_case(case))
        # Only reached if the suite ran to completion; a setup failure that
        # raises propagates before this line, so "Completed" is never misleading.
        _trace("[SUITE] Completed")
        return report

    def prepare_installed_batch(self) -> bool:
        # Once per batch: install the local APK, launch it, log in, then warm up
        # (never per case / retry). Returns True iff login succeeded; on failure
        # skip warm-up and don't proceed to verification.
        _trace("[INSTALLED BATCH] Installing local build")
        self._runner.install_local_build()
        _trace("[INSTALLED BATCH] Launching app before login")
        self._runner.open_installed_app()
        _trace("[INSTALLED BATCH] Ensuring login")
        if not self._runner.ensure_logged_in():
            _trace("[INSTALLED BATCH] login failed")
            return False
        self._runner.run_warm_up()
        return True

    def run_installed_batch(
        self, cases: Sequence[DeeplinkTestCase], report: SuiteReport
    ) -> None:
        _trace("[INSTALLED BATCH] Starting")
        if not self.prepare_installed_batch():
            # Batch login failed: record every case as a login-prep failure
            # (no _verify()), so each is FAIL like the uninstalled flow.
            for case in cases:
                _trace(f"[INSTALLED] {case.test_id} login failed")
                _trace(f"[INSTALLED] {case.test_id} skipping deeplink verification")
                report.results.append(_login_failed_result(case))
            return
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
        help="Maximum attempts per deeplink test case (default 2: 1 try + 1 retry).",
    )
    parser.add_argument(
        "--verify-timeout", type=float, default=DEFAULT_VERIFY_TIMEOUT_SECONDS,
        help=(
            "Maximum seconds to poll for the expected result after a deeplink "
            "before declaring a mismatch (default 15)."
        ),
    )
    parser.add_argument(
        "--no-warm-up", action="store_true",
        help="Skip the one-time first-install warm-up before the suite.",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Force a fresh officemobile build even if a prior APK exists.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv()
    args = _parse_args(argv)
    if args.max_attempts < 1:
        print("ERROR: --max-attempts must be at least 1", file=sys.stderr)
        return 2
    if args.verify_timeout < 0:
        print("ERROR: --verify-timeout must not be negative", file=sys.stderr)
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

    # Shared login capability: the same generic AppPilotAgent + Brain, reusing
    # the device's executor/observer (DRY). The executor's foreground check is
    # injected so login never completes while the app is not foreground.
    login_agent = build_login_agent(
        args.device,
        executor=executor,
        observer=observer,
        foreground_check=executor.is_foreground,
    )
    login_flow: LoginCapability = SharedLoginFlow(login_agent)

    # Build (or reuse) the local officemobile APK and install it via adb - never
    # from the actual Play Store.
    try:
        apk_path = officemobile_build.APK_PATH
        if args.rebuild or not apk_path.exists():
            _trace("[BUILD] building local officemobile APK")
            apk_path = officemobile_build.build_apk()
        _trace(f"[BUILD] using local APK: {apk_path}")
    except officemobile_build.BuildError as error:
        print(f"ERROR: could not build local APK: {error}", file=sys.stderr)
        return 2
    installer = LocalApkInstaller(executor, str(apk_path))
    runner = DeeplinkTestRunner(
        observer=observer,
        executor=executor,
        judge=judge,
        warm_up=warm_up,
        max_attempts=args.max_attempts,
        verify_timeout_seconds=args.verify_timeout,
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
