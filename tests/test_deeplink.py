"""Tests for the data-driven AppPilot deeplink test runner.

Covers Excel loading/parsing and required columns, deeplink/expected-result
extraction, the deterministic retry recipe (kill + 2s wait + relaunch), the
3-attempt bound, PASS on later attempts, FAIL after all mismatches, continuing
past failures, expected-failure-state matching, report generation, and the
one-time warm-up.
"""

import contextlib
import io
import json
import socket
import sys
import tempfile
import types
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import apppilot_agent as a  # noqa: E402
import deeplink_runner as d  # noqa: E402
from apppilot import email_report  # noqa: E402
from apppilot.preflight import device_check  # noqa: E402
from apppilot.preflight import emulator_autostart as emulator_node  # noqa: E402
from apppilot.preflight import maestro_check  # noqa: E402
from apppilot.preflight import build_tools_check  # noqa: E402
from apppilot.preflight import apk_source  # noqa: E402
from apppilot.preflight import results  # noqa: E402
from apppilot.preflight import model_check  # noqa: E402
from apppilot.preflight import credentials_check  # noqa: E402
from apppilot.preflight import python_check  # noqa: E402
from apppilot.preflight import path_setup  # noqa: E402
from apppilot.preflight.config_store import ConfigStore  # noqa: E402
from apppilot import preflight as preflight_node  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX = REPO_ROOT / "testcases" / "deeplinks" / "deeplink_tests.xlsx"


# --------------------------------------------------------------------------- #
# Helpers: build minimal in-memory .xlsx workbooks and fakes
# --------------------------------------------------------------------------- #
def _xlsx_bytes(rows, *, shared=None):
    """Build a tiny .xlsx. ``rows`` is a list of dict{col_letter: (type, value)}.

    type is 's' (shared), 'inlineStr' (inline), or '' (number/plain).
    """
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet_rows = []
    for r_index, row in enumerate(rows, start=1):
        cells = []
        for letter, (ctype, value) in row.items():
            ref = f"{letter}{r_index}"
            if ctype == "inlineStr":
                cells.append(
                    f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
                )
            elif ctype == "s":
                cells.append(f'<c r="{ref}" t="s"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        sheet_rows.append(f"<row r=\"{r_index}\">{''.join(cells)}</row>")
    sheet_xml = (
        f'<?xml version="1.0"?><worksheet {ns}><sheetData>'
        f"{''.join(sheet_rows)}</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if shared is not None:
            si = "".join(f"<si><t>{s}</t></si>" for s in shared)
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<?xml version="1.0"?><sst {ns} count="{len(shared)}" '
                f'uniqueCount="{len(shared)}">{si}</sst>',
            )
    return buf.getvalue()


def _write_xlsx(tmp_path, rows, *, shared=None, name="wb.xlsx"):
    path = tmp_path / name
    path.write_bytes(_xlsx_bytes(rows, shared=shared))
    return path


def _inline_row(a_val, b_val, c_val, d_val):
    return {
        "A": ("inlineStr", a_val),
        "B": ("inlineStr", b_val),
        "C": ("inlineStr", c_val),
        "D": ("inlineStr", d_val),
    }


def _case(test_id="TC001", expected="Chat screen"):
    return d.DeeplinkTestCase(
        test_id=test_id,
        deep_link="myapp://open/chat",
        user_type="Premium",
        expected_result=expected,
    )


class _StubObserver:
    def observe(self):
        return a.UIObservation(())


class _RecordingExecutor:
    """Records the ordered sequence of lifecycle calls for assertions."""

    def __init__(self):
        self.calls = []

    def open_link(self, deep_link):
        self.calls.append(("open_link", deep_link))

    def stop_app(self):
        self.calls.append(("stop_app", None))

    def launch_app(self):
        self.calls.append(("launch_app", None))


class _ScriptedJudge:
    """Returns the given verdicts in order (per evaluate call)."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.seen = []

    def evaluate(self, expected_result, observation):
        self.seen.append(expected_result)
        matched, reason = self._verdicts.pop(0)
        return d.ExpectationVerdict(matched=matched, reason=reason)


def _make_runner(judge, executor=None, warm_up=None, **kwargs):
    sleeps = []
    executor = executor or _RecordingExecutor()
    # Behaviour tests script exactly ONE judge verdict per attempt. A zero-length
    # verification window makes _verify observe/judge once per attempt; the
    # dedicated polling tests exercise multi-poll windows via an injected clock.
    kwargs.setdefault("verify_timeout_seconds", 0)
    kwargs.setdefault("verify_poll_interval_seconds", 0)
    runner = d.DeeplinkTestRunner(
        observer=_StubObserver(),
        executor=executor,
        judge=judge,
        warm_up=warm_up,
        sleep=lambda seconds: sleeps.append(seconds),
        settle_seconds=0,  # keep sleep records limited to the retry wait
        **kwargs,
    )
    return runner, executor, sleeps


# --------------------------------------------------------------------------- #
# Excel loading / parsing
# --------------------------------------------------------------------------- #
class ExcelLoadingTests(unittest.TestCase):
    def test_loads_bundled_workbook(self):
        # The bundled workbook is user-owned, so assert structural invariants,
        # not an exact layout: every row read, deep link + expected result kept
        # verbatim, deterministic Installed bool, and at least one quoted prompt.
        cases = d.load_deeplink_cases(REAL_XLSX)
        self.assertTrue(cases, "expected at least one case in the bundled workbook")
        ids = [c.test_id for c in cases]
        self.assertTrue(all(ids), "every case must have a non-empty test id")
        self.assertEqual(len(ids), len(set(ids)), "test ids must be unique")
        for case in cases:
            self.assertTrue(case.deep_link.startswith("https://"))
            self.assertTrue(case.expected_result.strip())
            self.assertIsInstance(case.installed, bool)
        with_prompt = [
            c for c in cases
            if c.expected_result.startswith("Chat screen with prompt")
            and "Summarize" in c.expected_result
        ]
        self.assertTrue(
            with_prompt,
            "expected a case whose Expected Screen quotes a concrete prompt",
        )

    def test_extracts_deeplink_and_expected_result(self):
        path = _write_xlsx(
            Path(self._tmp()),
            [_inline_row("TC010", "scheme://x/y", "Premium", "Researcher screen")],
        )
        cases = d.load_deeplink_cases(path)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].deep_link, "scheme://x/y")
        self.assertEqual(cases[0].expected_result, "Researcher screen")

    def test_skips_header_row(self):
        rows = [
            _inline_row("Test ID", "Deep Link", "User Type", "Expected Result"),
            _inline_row("TC001", "app://a", "Premium", "Chat screen"),
        ]
        cases = d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), rows))
        self.assertEqual([c.test_id for c in cases], ["TC001"])

    def test_supports_shared_strings(self):
        shared = ["TC777", "shared://link", "Premium", "Chat screen"]
        rows = [{
            "A": ("s", 0), "B": ("s", 1), "C": ("s", 2), "D": ("s", 3),
        }]
        cases = d.load_deeplink_cases(
            _write_xlsx(Path(self._tmp()), rows, shared=shared)
        )
        self.assertEqual(cases[0].deep_link, "shared://link")

    def test_missing_required_column_raises(self):
        # Expected Result missing -> required-column error.
        rows = [{
            "A": ("inlineStr", "TC001"),
            "B": ("inlineStr", "app://a"),
            "C": ("inlineStr", "Premium"),
        }]
        with self.assertRaises(ValueError) as ctx:
            d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), rows))
        self.assertIn("Expected Result", str(ctx.exception))

    def test_empty_sheet_raises(self):
        with self.assertRaises(ValueError):
            d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), []))

    def _tmp(self):
        import tempfile
        return tempfile.mkdtemp()


# --------------------------------------------------------------------------- #
# Semantic match / retry behaviour
# --------------------------------------------------------------------------- #
class RunnerBehaviourTests(unittest.TestCase):
    def test_semantic_match_passes_on_first_attempt(self):
        judge = _ScriptedJudge([(True, "chat screen shown")])
        runner, executor, sleeps = _make_runner(judge)
        report = runner.run([_case()])
        result = report.results[0]
        self.assertTrue(result.passed)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.passing_attempt, 1)
        # No retry -> no stop_app during attempts, then one case-cleanup stop.
        self.assertEqual(
            executor.calls,
            [("open_link", "myapp://open/chat"), ("stop_app", None)],
        )
        self.assertEqual(sleeps, [])

    def test_mismatch_triggers_kill_wait_relaunch(self):
        judge = _ScriptedJudge([(False, "wrong screen"), (True, "now correct")])
        runner, executor, sleeps = _make_runner(judge)
        report = runner.run([_case()])
        result = report.results[0]
        self.assertTrue(result.passed)
        self.assertEqual(result.passing_attempt, 2)
        # Exact retry recipe between attempt 1 and 2: kill -> wait 2s -> relaunch,
        # then a final case-cleanup stop when the case finishes.
        self.assertEqual(
            executor.calls,
            [
                ("open_link", "myapp://open/chat"),
                ("stop_app", None),
                ("open_link", "myapp://open/chat"),
                ("stop_app", None),
            ],
        )
        self.assertEqual(sleeps, [d.DEFAULT_RETRY_WAIT_SECONDS])

    def test_maximum_three_attempts_then_fail(self):
        judge = _ScriptedJudge([(False, "no"), (False, "no"), (False, "no")])
        runner, executor, sleeps = _make_runner(judge, max_attempts=3)
        report = runner.run([_case()])
        result = report.results[0]
        self.assertFalse(result.passed)
        self.assertEqual(len(result.attempts), 3)
        # Exactly two retries (before attempts 2 and 3): two kills, two waits,
        # then a final case-cleanup stop when the case finishes.
        self.assertEqual(
            [c[0] for c in executor.calls],
            ["open_link", "stop_app", "open_link", "stop_app", "open_link",
             "stop_app"],
        )
        self.assertEqual(sleeps, [d.DEFAULT_RETRY_WAIT_SECONDS] * 2)

    def test_default_is_one_retry_then_fail(self):
        # A failed test is retried exactly once by default (2 total attempts).
        judge = _ScriptedJudge([(False, "no"), (False, "no")])
        runner, executor, sleeps = _make_runner(judge)
        report = runner.run([_case()])
        result = report.results[0]
        self.assertFalse(result.passed)
        self.assertEqual(d.DEFAULT_MAX_ATTEMPTS, 2)
        self.assertEqual(len(result.attempts), 2)
        # Exactly one retry (before attempt 2): one kill, one wait, then a final
        # case-cleanup stop when the case finishes.
        self.assertEqual(
            [c[0] for c in executor.calls],
            ["open_link", "stop_app", "open_link", "stop_app"],
        )
        self.assertEqual(sleeps, [d.DEFAULT_RETRY_WAIT_SECONDS])

    def test_pass_on_third_attempt(self):
        judge = _ScriptedJudge([(False, "no"), (False, "no"), (True, "yes")])
        runner, _, _ = _make_runner(judge, max_attempts=3)
        report = runner.run([_case()])
        self.assertTrue(report.results[0].passed)
        self.assertEqual(report.results[0].passing_attempt, 3)
        self.assertEqual(len(report.results[0].attempts), 3)

    def test_expected_failure_state_is_a_pass(self):
        # Expected result is an error state; the judge observes exactly that.
        judge = _ScriptedJudge([(True, "observed the expected error screen")])
        runner, _, _ = _make_runner(judge)
        case = _case(expected="Error screen: content unavailable")
        report = runner.run([case])
        self.assertTrue(report.results[0].passed)

    def test_continues_to_next_case_after_failure(self):
        judge = _ScriptedJudge(
            [(False, "no"), (False, "no"), (False, "no"), (True, "second ok")]
        )
        runner, _, _ = _make_runner(judge)
        report = runner.run([_case("TC001"), _case("TC002")])
        self.assertEqual(len(report.results), 2)
        self.assertFalse(report.results[0].passed)
        self.assertTrue(report.results[1].passed)


# --------------------------------------------------------------------------- #
# Warm-up
# --------------------------------------------------------------------------- #
class WarmUpTests(unittest.TestCase):
    def test_warm_up_runs_once_for_whole_suite(self):
        counter = {"n": 0}

        def warm_up():
            counter["n"] += 1

        judge = _ScriptedJudge(
            [(False, "no"), (False, "no"), (False, "no"), (True, "ok")]
        )
        runner, _, _ = _make_runner(judge, warm_up=warm_up)
        runner.run([_case("TC001"), _case("TC002")])
        # Called once total, despite two cases and three retries in the first.
        self.assertEqual(counter["n"], 1)

    def test_no_warm_up_when_none(self):
        judge = _ScriptedJudge([(True, "ok")])
        runner, _, _ = _make_runner(judge, warm_up=None)
        runner.run([_case()])  # must not raise

    def test_maestro_warm_up_runs_full_launch_wait_stop_each_cycle(self):
        # Record executor calls and sleeps into one ordered list to prove the
        # exact interleaving. With launches=3 every cycle must be a full
        # launch -> wait -> stop (no cycle skips the stop).
        events = []
        executor = _RecordingExecutor()
        executor.calls = events  # share the single ordered event log
        warm = d.MaestroWarmUp(
            executor,
            launches=3,
            settle_seconds=3,
            sleep=lambda seconds: events.append(("sleep", seconds)),
        )
        warm()
        self.assertEqual(
            events,
            [
                ("launch_app", None), ("sleep", 3), ("stop_app", None),
                ("launch_app", None), ("sleep", 3), ("stop_app", None),
                ("launch_app", None), ("sleep", 3), ("stop_app", None),
            ],
        )

    def test_maestro_warm_up_launch_and_stop_counts_match(self):
        executor = _RecordingExecutor()
        warm = d.MaestroWarmUp(
            executor, launches=3, settle_seconds=0, sleep=lambda s: None
        )
        warm()
        launches = [c for c in executor.calls if c[0] == "launch_app"]
        stops = [c for c in executor.calls if c[0] == "stop_app"]
        self.assertEqual(len(launches), 3)
        self.assertEqual(len(stops), 3)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
class ReportTests(unittest.TestCase):
    def test_report_totals_and_lines(self):
        judge = _ScriptedJudge(
            [
                (True, "ok"),                         # TC001 attempt 1 pass
                (False, "no"), (True, "ok"),          # TC002 attempt 2 pass
                (False, "a"), (False, "b"), (False, "c"),  # TC003 all fail
            ]
        )
        runner, _, _ = _make_runner(judge, max_attempts=3)
        report = runner.run([_case("TC001"), _case("TC002"), _case("TC003")])
        text = report.format()
        self.assertEqual(report.total, 3)
        self.assertEqual(report.passed, 2)
        self.assertEqual(report.failed, 1)
        self.assertIn("TC001  PASS  Attempt 1", text)
        self.assertIn("TC002  PASS  Attempt 2", text)
        self.assertIn("TC003  FAIL  Attempt 3", text)
        self.assertIn("Total test cases: 3", text)
        self.assertIn("Deeplink test cases passed: 2", text)
        self.assertIn("Deeplink test cases failed: 1", text)
        # Failure reason for the last attempt of the failing case is included.
        self.assertIn("mismatch - c", text)


# --------------------------------------------------------------------------- #
# Judge (semantic evaluation via injected transport)
# --------------------------------------------------------------------------- #
class JudgeTests(unittest.TestCase):
    def _judge(self, response_content):
        return d.LLMExpectationJudge(
            model="m",
            api_key="k",
            transport=lambda payload: {
                "choices": [{"message": {"content": response_content}}]
            },
        )

    def test_match_true(self):
        judge = self._judge('{"match": true, "reason": "chat screen"}')
        verdict = judge.evaluate("Chat screen", a.UIObservation(()))
        self.assertTrue(verdict.matched)
        self.assertEqual(verdict.reason, "chat screen")

    def test_match_false(self):
        judge = self._judge('{"match": false, "reason": "different screen"}')
        verdict = judge.evaluate("Chat screen", a.UIObservation(()))
        self.assertFalse(verdict.matched)

    def test_unparseable_response_is_mismatch(self):
        judge = self._judge("not json")
        verdict = judge.evaluate("Chat screen", a.UIObservation(()))
        self.assertFalse(verdict.matched)

    def test_specific_expected_prompt_is_enforced(self):
        # A specific/quoted expected prompt must require THAT prompt's content -
        # a generic or different "chat with a prompt" (e.g. an onboarding
        # suggested prompt, or a chat login wandered into) must not false-pass.
        system_prompt = d.LLMExpectationJudge._SYSTEM_PROMPT.lower()
        self.assertIn("same", system_prompt)
        self.assertIn("mismatch", system_prompt)
        self.assertIn("suggested", system_prompt)
        self.assertIn("different prompt", system_prompt)

    def test_expected_prompt_text_reaches_model_verbatim(self):
        # The specific expected prompt from Excel must be handed to the judge so
        # it can require that exact content (not just "some prompt present").
        expected = 'Chat screen with prompt "Summarize the top three news"'
        rendered = d.LLMExpectationJudge._render(expected, a.UIObservation(()))
        self.assertIn(expected, rendered)

    def test_credential_values_never_reach_judge_prompt(self):
        # A password field's live text is redacted by the observer, so the
        # rendered judge prompt cannot contain the secret.
        secret = "Growth@2026"
        node = {
            "attributes": {
                "resource-id": "com.microsoft:id/i0118",
                "class": "android.widget.EditText",
                "hintText": "Password",
                "text": secret,
                "enabled": "true",
            }
        }
        observer = a.MaestroHierarchyObserver("device")
        elements = []
        observer._collect(node, (), None, False, elements)
        observation = a.UIObservation(tuple(elements))
        rendered = d.LLMExpectationJudge._render("Chat screen", observation)
        self.assertNotIn(secret, rendered)


# --------------------------------------------------------------------------- #
# Installed vs uninstalled orchestration (incremental feature)
# --------------------------------------------------------------------------- #
def _icase(test_id="TC001", expected="Chat screen"):
    """An INSTALLED=True deeplink case."""
    return d.DeeplinkTestCase(
        test_id=test_id,
        deep_link="myapp://open/chat",
        user_type="Premium",
        expected_result=expected,
        installed=True,
    )


def _ucase(test_id="TC900", expected="Chat screen"):
    """An INSTALLED=False (first-open-after-install) deeplink case."""
    return d.DeeplinkTestCase(
        test_id=test_id,
        deep_link="myapp://open/chat",
        user_type="Premium",
        expected_result=expected,
        installed=False,
    )


class _FakeLogin:
    """Records how many times the shared login capability was invoked."""

    def __init__(self):
        self.ready_calls = 0

    def ensure_ready(self):
        self.ready_calls += 1
        return True  # login preparation succeeded


class _FakeInstaller:
    """Records the fresh-install lifecycle for the uninstalled scenario."""

    def __init__(self, fail_times=0, preinstalled=False):
        self.absent_calls = 0
        self.install_calls = 0
        self.fresh_calls = 0
        self.open_calls = 0
        self._fail_times = fail_times
        self._preinstalled = preinstalled

    def ensure_absent(self):
        self.absent_calls += 1
        was_installed = self._preinstalled
        self._preinstalled = False
        return was_installed

    def install_fresh(self):
        self.fresh_calls += 1

    def open(self):
        self.open_calls += 1

    def install_and_open(self, via_store_button=False):
        self.install_calls += 1
        if self.install_calls <= self._fail_times:
            raise RuntimeError("install did not complete")


class _CountingWarmUp:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1


class InstalledOrchestrationTests(unittest.TestCase):
    def test_installed_batch_warms_up_once_for_all_cases(self):
        judge = _ScriptedJudge([(True, "ok")] * 3)
        warm = _CountingWarmUp()
        login = _FakeLogin()
        runner, _, _ = _make_runner(
            judge, warm_up=warm, login_flow=login
        )
        report = runner.run([_icase("T1"), _icase("T2"), _icase("T3")])
        self.assertEqual(warm.n, 1)  # once for the whole batch, not per case
        self.assertEqual(login.ready_calls, 1)
        self.assertEqual(report.passed, 3)

    def test_warm_up_and_login_happen_before_first_installed_case(self):
        order = []
        judge = _ScriptedJudge([(True, "ok")])

        class _OrderLogin:
            def ensure_ready(self_inner):
                order.append("login")
                return True  # login preparation succeeded

        def warm_up():
            order.append("warm_up")

        executor = _RecordingExecutor()
        original_open = executor.open_link

        def open_link(deep_link):
            order.append("open_link")
            original_open(deep_link)

        executor.open_link = open_link
        runner, _, _ = _make_runner(
            judge, executor=executor, warm_up=warm_up, login_flow=_OrderLogin()
        )
        runner.run([_icase()])
        self.assertEqual(order, ["login", "warm_up", "open_link"])

    def test_installed_retry_does_not_repeat_warm_up(self):
        judge = _ScriptedJudge([(False, "no"), (False, "no"), (False, "no")])
        warm = _CountingWarmUp()
        runner, executor, _ = _make_runner(judge, warm_up=warm, max_attempts=3)
        report = runner.run([_icase()])
        self.assertEqual(warm.n, 1)  # not repeated across the 3 attempts
        self.assertFalse(report.results[0].passed)
        # Installed retry recipe is still kill -> wait -> reopen, plus a final
        # case-cleanup stop when the case finishes.
        self.assertEqual(
            [c[0] for c in executor.calls],
            ["open_link", "stop_app", "open_link", "stop_app", "open_link",
             "stop_app"],
        )


class UninstalledOrchestrationTests(unittest.TestCase):
    def test_uninstalled_case_does_not_run_warm_up(self):
        judge = _ScriptedJudge([(True, "ok")])
        warm = _CountingWarmUp()
        installer = _FakeInstaller()
        login = _FakeLogin()
        runner, _, _ = _make_runner(
            judge, warm_up=warm, installer=installer, login_flow=login
        )
        runner.run([_ucase()])
        self.assertEqual(warm.n, 0)  # never warmed up for a fresh-install case
        self.assertEqual(installer.absent_calls, 2)  # 1 suite-startup + 1 per-attempt
        self.assertEqual(installer.install_calls, 1)
        self.assertEqual(login.ready_calls, 1)

    def test_uninstalled_reestablishes_fresh_state_each_attempt(self):
        judge = _ScriptedJudge([(False, "no"), (False, "no"), (False, "no")])
        installer = _FakeInstaller()
        runner, executor, _ = _make_runner(
            judge, installer=installer, max_attempts=3
        )
        report = runner.run([_ucase()])
        self.assertFalse(report.results[0].passed)
        self.assertEqual(len(report.results[0].attempts), 3)
        # Every attempt genuinely uninstalls + installs (no kill/wait recipe that
        # would leave the app installed and degrade a retry into installed).
        self.assertEqual(installer.absent_calls, 4)  # 1 suite-startup + 3 attempts
        self.assertEqual(installer.install_calls, 3)
        self.assertEqual([c[0] for c in executor.calls], ["open_link"] * 3)
        self.assertNotIn("stop_app", [c[0] for c in executor.calls])

    def test_uninstalled_opens_exact_deeplink(self):
        judge = _ScriptedJudge([(True, "ok")])
        installer = _FakeInstaller()
        runner, executor, _ = _make_runner(judge, installer=installer)
        runner.run([_ucase()])
        # The deeplink routes to the store; the app is then launched by tapping
        # the store's "Open" button (installer-side), so the executor fires the
        # deeplink exactly once, verbatim.
        self.assertEqual(executor.calls, [("open_link", "myapp://open/chat")])

    def test_install_failure_is_retryable_not_fatal(self):
        # First attempt's install fails; the suite must not crash - it records a
        # failed attempt and retries with fresh state, passing on a later attempt.
        judge = _ScriptedJudge([(True, "ok")])  # only reached after install works
        installer = _FakeInstaller(fail_times=1)
        runner, _, _ = _make_runner(judge, installer=installer)
        report = runner.run([_ucase()])
        self.assertTrue(report.results[0].passed)
        self.assertEqual(installer.install_calls, 2)  # failed once, then succeeded
        self.assertEqual(installer.absent_calls, 3)  # 1 suite-startup + 2 attempts

    def test_persistent_install_failure_fails_case_without_crashing_suite(self):
        judge = _ScriptedJudge([])  # never consulted: setup fails every attempt
        installer = _FakeInstaller(fail_times=99)
        runner, _, _ = _make_runner(judge, installer=installer, max_attempts=3)
        report = runner.run([_ucase()])
        self.assertFalse(report.results[0].passed)
        self.assertEqual(len(report.results[0].attempts), 3)  # all attempts tried
        self.assertEqual(installer.install_calls, 3)

    def test_suite_uninstalls_once_at_startup_for_installed_only_batch(self):
        # One-time clean state at suite startup even when every case is installed:
        # exactly one ensure_absent, regardless of the per-batch install/warm-up.
        judge = _ScriptedJudge([(True, "ok")])
        installer = _FakeInstaller()
        login = _FakeLogin()
        runner, _, _ = _make_runner(
            judge, warm_up=_CountingWarmUp(), installer=installer, login_flow=login
        )
        runner.run([_icase("T1")])
        self.assertEqual(installer.absent_calls, 1)  # suite-startup cleanup only

    def test_suite_startup_logs_when_existing_app_is_removed(self):
        judge = _ScriptedJudge([(True, "ok")])
        installer = _FakeInstaller(preinstalled=True)  # an old app is present
        runner, _, _ = _make_runner(judge, installer=installer)
        _, out = _capture(runner.run, [_icase("T1")])
        self.assertIn("[SUITE] Removed existing app install", out)

    def test_suite_startup_logs_when_no_existing_app(self):
        judge = _ScriptedJudge([(True, "ok")])
        installer = _FakeInstaller()  # device already clean
        runner, _, _ = _make_runner(judge, installer=installer)
        _, out = _capture(runner.run, [_icase("T1")])
        self.assertIn("[SUITE] No existing app install to remove", out)


class MixedBatchTests(unittest.TestCase):
    def test_mixed_installed_and_uninstalled(self):
        # Installed case is judged first (batch), then the uninstalled case.
        judge = _ScriptedJudge([(True, "installed ok"), (True, "fresh ok")])
        warm = _CountingWarmUp()
        installer = _FakeInstaller()
        login = _FakeLogin()
        runner, _, _ = _make_runner(
            judge, warm_up=warm, installer=installer, login_flow=login
        )
        report = runner.run([_icase("T1"), _ucase("T2")])
        self.assertEqual([r.case.test_id for r in report.results], ["T1", "T2"])
        self.assertTrue(all(r.passed for r in report.results))
        self.assertEqual(warm.n, 1)  # only for the installed batch
        self.assertEqual(installer.absent_calls, 2)  # 1 suite-startup + 1 uninstalled case
        # Shared login used by both scenarios: once for the batch + once for the
        # single uninstalled attempt.
        self.assertEqual(login.ready_calls, 2)


class InstalledColumnLoadingTests(unittest.TestCase):
    def _tmp(self):
        import tempfile
        return tempfile.mkdtemp()

    def _row5(self, a_val, b_val, c_val, d_val, e_val):
        return {
            "A": ("inlineStr", a_val),
            "B": ("inlineStr", b_val),
            "C": ("inlineStr", c_val),
            "D": ("inlineStr", d_val),
            "E": ("inlineStr", e_val),
        }

    def test_app_store_deeplink_is_uninstalled_by_default(self):
        rows = [
            _inline_row(
                "TC1",
                "https://m365.cloud.microsoft/?openAppStoreOnLoad=true",
                "Premium",
                "Chat screen",
            )
        ]
        cases = d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), rows))
        self.assertFalse(cases[0].installed)

    def test_normal_deeplink_is_installed_by_default(self):
        rows = [_inline_row("TC1", "app://open", "Premium", "Chat screen")]
        cases = d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), rows))
        self.assertTrue(cases[0].installed)

    def test_explicit_installed_column_overrides(self):
        rows = [
            self._row5("Test ID", "Deep Link", "License", "Expected", "Installed"),
            self._row5(
                "TC1",
                "https://x/?openAppStoreOnLoad=true",
                "Premium",
                "Chat screen",
                "yes",
            ),
            self._row5("TC2", "app://open", "Premium", "Chat screen", "no"),
        ]
        cases = d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), rows))
        # Explicit column wins over the deeplink-derived default in both rows.
        self.assertTrue(cases[0].installed)
        self.assertFalse(cases[1].installed)

    def test_leading_title_row_is_skipped(self):
        # Mirrors the real workbook: a merged title row above the header row.
        rows = [
            {"A": ("inlineStr", "Test Case Reference")},
            _inline_row("Test Case ID", "Launch URL", "License", "Expected Screen"),
            _inline_row("TC001", "https://m365/chat", "Premium", "Chat screen"),
        ]
        cases = d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), rows))
        self.assertEqual([c.test_id for c in cases], ["TC001"])
        self.assertEqual(cases[0].user_type, "Premium")
        self.assertEqual(cases[0].expected_result, "Chat screen")

    def test_reordered_columns_mapped_by_header(self):
        # Columns intentionally shuffled; header names drive the mapping.
        rows = [
            _inline_row("Expected Screen", "Test Case ID", "Launch URL", "License"),
            _inline_row("Chat screen", "TC001", "app://go", "Premium"),
        ]
        cases = d.load_deeplink_cases(_write_xlsx(Path(self._tmp()), rows))
        self.assertEqual(cases[0].test_id, "TC001")
        self.assertEqual(cases[0].deep_link, "app://go")
        self.assertEqual(cases[0].expected_result, "Chat screen")
        self.assertEqual(cases[0].user_type, "Premium")


class LocalApkInstallerTests(unittest.TestCase):
    class _FakeExec:
        def __init__(self, foreground_after=0):
            self.events = []
            # Number of is_foreground() calls that return False before the app is
            # reported foreground (0 = foreground immediately).
            self._foreground_after = foreground_after
            self._foreground_calls = 0

        def ensure_uninstalled(self):
            self.events.append("uninstall")

        def install_apk(self, apk_path, timeout=300):
            self.events.append(("install_apk", apk_path))

        def launch_app_via_adb(self, timeout=30):
            self.events.append("launch_app_via_adb")

        def launch_app_via_open_btn_click(self, timeout=60):
            self.events.append("launch_app_via_open_btn_click")

        def is_foreground(self, timeout=15):
            self.events.append("is_foreground")
            self._foreground_calls += 1
            return self._foreground_calls > self._foreground_after

    def _installer(self, exe, **kw):
        kw.setdefault("foreground_poll_seconds", 0)
        return d.LocalApkInstaller(exe, "/tmp/officemobile.apk", **kw)

    def test_ensure_absent_install_and_open_sequence(self):
        # Uninstall -> adb install the local APK -> launch the app via the adb
        # CLI (NOT the store's "Open" button, NOT re-firing the deeplink) ->
        # confirm the target app is actually foreground.
        exe = self._FakeExec()
        installer = self._installer(exe)
        installer.ensure_absent()
        installer.install_and_open()
        self.assertEqual(
            exe.events,
            [
                "uninstall",
                ("install_apk", "/tmp/officemobile.apk"),
                "launch_app_via_adb",
                "is_foreground",
            ],
        )

    def test_install_and_open_via_store_button_sequence(self):
        # Uninstalled flow: adb install the local APK -> open by tapping the
        # store's "Open" button via Maestro -> confirm the app is foreground.
        exe = self._FakeExec()
        installer = self._installer(exe)
        installer.install_and_open(via_store_button=True)
        self.assertEqual(
            exe.events,
            [
                ("install_apk", "/tmp/officemobile.apk"),
                "launch_app_via_open_btn_click",
                "is_foreground",
            ],
        )

    def test_store_button_open_retaps_until_foreground(self):
        # A store Open tap returning is NOT proof the app opened: while not yet
        # foreground the installer re-taps Open (best-effort) and polls again.
        exe = self._FakeExec(foreground_after=2)
        self._installer(exe).install_and_open(via_store_button=True)
        self.assertEqual(
            exe.events,
            [
                ("install_apk", "/tmp/officemobile.apk"),
                "launch_app_via_open_btn_click",
                "is_foreground",
                "launch_app_via_open_btn_click",
                "is_foreground",
                "launch_app_via_open_btn_click",
                "is_foreground",
            ],
        )

    def test_install_and_open_waits_and_relaunches_until_foreground(self):
        # A launch command returning is NOT proof the app opened: while the app
        # is not yet foreground the installer re-launches via adb (best-effort)
        # and polls again, only returning once the foreground check passes.
        exe = self._FakeExec(foreground_after=2)
        self._installer(exe).install_and_open()
        self.assertEqual(
            exe.events,
            [
                ("install_apk", "/tmp/officemobile.apk"),
                "launch_app_via_adb",
                "is_foreground",
                "launch_app_via_adb",
                "is_foreground",
                "launch_app_via_adb",
                "is_foreground",
            ],
        )

    def test_install_and_open_fails_if_never_foreground(self):
        # If the app never becomes foreground within the bounded window, the
        # attempt fails cleanly (raises) rather than handing a not-yet-open UI to
        # login. "app opened" must never be reported in this case.
        exe = self._FakeExec(foreground_after=10_000)
        installer = self._installer(
            exe, foreground_timeout_seconds=0, foreground_poll_seconds=0
        )
        with self.assertRaises(RuntimeError):
            installer.install_and_open()
        # It launched the app and checked foreground, then failed - it never
        # silently proceeded as if the app had opened.
        self.assertIn("launch_app_via_adb", exe.events)
        self.assertIn("is_foreground", exe.events)

    def test_install_fresh_installs_via_adb_only(self):
        # The installed batch uses install_fresh to put the local build on the
        # device up front - install only, no launch.
        exe = self._FakeExec()
        self._installer(exe).install_fresh()
        self.assertEqual(exe.events, [("install_apk", "/tmp/officemobile.apk")])


class PlayStoreInstallerTests(unittest.TestCase):
    class _FakeExec:
        def __init__(self, *, installed_after=0, foreground_after=0):
            self.events = []
            # is_installed() returns False until this many calls have been made
            # (0 = already installed on the very first check).
            self._installed_after = installed_after
            self._installed_calls = 0
            self._foreground_after = foreground_after
            self._foreground_calls = 0

        def is_installed(self):
            self.events.append("is_installed")
            self._installed_calls += 1
            return self._installed_calls > self._installed_after

        def open_store_page(self, timeout=60):
            self.events.append("open_store_page")

        def tap_store_install_button(self, timeout=120):
            self.events.append("tap_store_install_button")

        def launch_app_via_adb(self, timeout=30):
            self.events.append("launch_app_via_adb")

        def launch_app_via_open_btn_click(self, timeout=60):
            self.events.append("launch_app_via_open_btn_click")

        def is_foreground(self, timeout=15):
            self.events.append("is_foreground")
            self._foreground_calls += 1
            return self._foreground_calls > self._foreground_after

    def _installer(self, exe, **kw):
        kw.setdefault("foreground_poll_seconds", 0)
        kw.setdefault("install_poll_seconds", 0)
        return d.PlayStoreInstaller(exe, **kw)

    def test_install_fresh_installs_from_store_when_missing(self):
        # App absent -> open the store page -> tap Install -> poll pm until the
        # package is really installed. No adb install, no local APK.
        exe = self._FakeExec(installed_after=1)
        self._installer(exe).install_fresh()
        self.assertEqual(
            exe.events,
            [
                "is_installed",  # guard: not installed yet
                "open_store_page",
                "tap_store_install_button",
                "is_installed",  # poll: now installed
            ],
        )

    def test_install_fresh_is_noop_when_already_installed(self):
        # Idempotent: an already-installed app is left as-is (no store tap).
        exe = self._FakeExec(installed_after=0)
        self._installer(exe).install_fresh()
        self.assertEqual(exe.events, ["is_installed"])

    def test_install_fresh_polls_until_installed(self):
        # The Install tap returning is NOT proof the install finished: poll pm
        # until the package appears.
        exe = self._FakeExec(installed_after=3)
        self._installer(exe).install_fresh()
        self.assertEqual(
            exe.events,
            [
                "is_installed",
                "open_store_page",
                "tap_store_install_button",
                "is_installed",
                "is_installed",
                "is_installed",
            ],
        )

    def test_install_fresh_fails_if_never_installs(self):
        # If the package never appears within the bounded window, fail cleanly.
        exe = self._FakeExec(installed_after=10_000)
        installer = self._installer(
            exe, install_timeout_seconds=0, install_poll_seconds=0
        )
        with self.assertRaises(RuntimeError):
            installer.install_fresh()

    def test_ensure_absent_never_uninstalls(self):
        # A store app is never uninstalled (no local APK to put it back).
        exe = self._FakeExec()
        self.assertFalse(self._installer(exe).ensure_absent())
        self.assertEqual(exe.events, [])

    def test_install_and_open_installs_then_launches(self):
        exe = self._FakeExec(installed_after=0)
        self._installer(exe).install_and_open()
        self.assertEqual(
            exe.events,
            ["is_installed", "launch_app_via_adb", "is_foreground"],
        )

    def test_open_never_taps_store_button(self):
        # Even when the uninstalled batch asks for the store "Open" button, the
        # store app is already installed (no store window), so we always launch
        # via adb - never stall tapping a button that will never appear.
        exe = self._FakeExec(installed_after=0)
        self._installer(exe).install_and_open(via_store_button=True)
        self.assertNotIn("launch_app_via_open_btn_click", exe.events)
        self.assertEqual(
            exe.events,
            ["is_installed", "launch_app_via_adb", "is_foreground"],
        )


class SuggestedPromptGuardTests(unittest.TestCase):
    def _element(self, *, text="", resource_id="", is_input=False):
        return a.UIElement(
            element_id="e",
            parent_id=None,
            text=text,
            accessibility_text="",
            hint_text="",
            resource_id=resource_id,
            class_name="",
            clickable=False,
            enabled=True,
            is_input=is_input,
            label="",
        )

    def test_intro_suggested_prompt_is_not_the_goal(self):
        evaluator = a.SignedInCopilotGoalEvaluator()
        observation = a.UIObservation(
            (self._element(text="Let's get started"),)
        )
        # Must NOT report PASS on the suggested-prompt intro (so the agent closes
        # it via X instead of sending the suggested prompt).
        self.assertFalse(evaluator.is_reached("goal", observation))

    def test_signed_in_composer_is_the_goal(self):
        evaluator = a.SignedInCopilotGoalEvaluator()
        observation = a.UIObservation(
            (self._element(text="Message Copilot"),)
        )
        self.assertTrue(evaluator.is_reached("goal", observation))


class _RecordingBackProvider:
    """Counts Brain calls (recording each observation) and takes a safe Back."""

    def __init__(self):
        self.calls = 0
        self.observations = []

    def decide(self, request):
        self.calls += 1
        self.observations.append(request.observation)
        return a.ModelDecision(
            action=a.Action(a.ActionKind.PRESS_BACK), reason="advance"
        )


def _cred_el():
    # A password field => SafetyValidator/evaluator see a credential field.
    return a.UIElement(
        element_id="e", parent_id=None, text="", accessibility_text="",
        hint_text="Password", resource_id="i0118", class_name="EditText",
        clickable=True, enabled=True, is_input=True, label="",
    )


def _intro_el():
    # The initial suggested-prompt/welcome interruption (actionable: has an X).
    return _ui_el(
        text="Let's get started", resource_id="intro_suggestion", clickable=True
    )


def _post_intro_el():
    # A generic post-onboarding screen: NOT intro, NOT a composer, NOT a
    # credential field. Belongs to the caller/deeplink test.
    return _ui_el(text="Some content", resource_id="content_view", clickable=True)


def _ask_copilot_el():
    # An "Ask Copilot" affordance the login agent must NEVER tap after boundary.
    return _ui_el(text="Ask Copilot", resource_id="ask_copilot_btn", clickable=True)


def _composer_home_el():
    return _ui_el(text="Message Copilot", resource_id="copilot_composer")


def _chat_screen_el():
    # Authenticated Copilot Chat screen (a composer/home) - a login terminal.
    return _composer_home_el()


def _search_screen_el():
    # Authenticated Search screen: a real search box. Not a sign-in blocker, not
    # the intro interruption, not a loading screen - a valid login terminal.
    return _ui_el(
        text="Search", resource_id="search_box", is_input=True, clickable=True
    )


def _welcome_landing_el():
    # The fresh-install, logged-OUT welcome screen: "Continue with Microsoft".
    return _ui_el(
        text="Continue with Microsoft",
        resource_id="signin_microsoft",
        clickable=True,
    )


class LoginBoundaryTests(unittest.TestCase):
    """The shared login capability is a PREPARATION step: it stops at the
    authentication + initial-onboarding boundary and returns control. It must not
    navigate into Copilot to make its own goal true."""

    def _eval(self):
        return a.SignedInCopilotGoalEvaluator()

    # -- evaluator-level boundary contract ---------------------------------- #
    def test_not_reached_while_credential_field_present(self):
        ev = self._eval()
        self.assertFalse(ev.is_reached("g", a.UIObservation((_cred_el(),))))

    def test_intro_present_is_not_the_goal_but_optional(self):
        ev = self._eval()
        # While present, the intro interruption is not the goal (dismiss first).
        self.assertFalse(ev.is_reached("g", a.UIObservation((_intro_el(),))))
        # It is OPTIONAL: once gone, a plain authenticated app screen completes -
        # no memory of "having seen an intro" is required.
        self.assertTrue(
            ev.is_reached("g", a.UIObservation((_post_intro_el(),)))
        )

    def test_boundary_reached_immediately_after_intro_dismissed(self):
        ev = self._eval()
        ev.is_reached("g", a.UIObservation((_intro_el(),)))  # intro shown
        # The very next screen (even without a recognizable composer) is the
        # boundary - login stops here rather than navigating further.
        self.assertTrue(
            ev.is_reached("g", a.UIObservation((_ask_copilot_el(),)))
        )

    def test_already_signed_in_composer_is_boundary_without_intro(self):
        ev = self._eval()
        self.assertTrue(
            ev.is_reached("g", a.UIObservation((_composer_home_el(),)))
        )

    def test_chat_screen_is_a_login_terminal(self):
        # Login terminal state: the authenticated Chat screen completes at once.
        ev = self._eval()
        self.assertTrue(
            ev.is_reached("g", a.UIObservation((_chat_screen_el(),)))
        )

    def test_search_screen_is_a_login_terminal(self):
        # Login terminal state: the authenticated Search screen completes at once
        # (via the generic "authenticated + inside app" rule - no hardcoded
        # Search/Chat strings, so it can never regress into pressing Back).
        ev = self._eval()
        self.assertTrue(
            ev.is_reached("g", a.UIObservation((_search_screen_el(),)))
        )

    def test_authenticated_app_screen_is_reached_without_intro(self):
        # THE MISSING STATE (live bug): authenticated, no sign-in blocker, no
        # intro, just a normal actionable app screen. This is login-complete
        # IMMEDIATELY - the evaluator must NOT withhold PASS (which previously let
        # the Brain choose "press back" to hunt for onboarding).
        ev = self._eval()
        self.assertTrue(
            ev.is_reached("g", a.UIObservation((_post_intro_el(),)))
        )

    def test_signin_button_screen_is_not_reached(self):
        # A welcome screen with a tappable "Sign in" (no credential field yet)
        # is still NOT authenticated -> not reached.
        ev = self._eval()
        signin = _ui_el(text="Sign in", resource_id="signin_button", clickable=True)
        self.assertFalse(ev.is_reached("g", a.UIObservation((signin,))))

    def test_fresh_install_welcome_screen_is_not_reached(self):
        # Live bug: fresh install lands on the logged-OUT welcome screen
        # ("Continue with Microsoft"/...). Even the deterministic fallback
        # recognizes the generic federated "continue with" sign-in affordance, so
        # login must NOT report already-signed-in here.
        ev = self._eval()
        terms = _ui_el(text="Terms of use", resource_id="", clickable=True)
        obs = a.UIObservation((_welcome_landing_el(), terms))
        self.assertFalse(ev.is_reached("g", obs))

    def test_transient_loading_screen_is_not_reached(self):
        # No actionable UI (authenticated but loading/transitioning): withhold
        # PASS so the agent's generic wait re-observes; do NOT complete here.
        ev = self._eval()
        loading = _ui_el(text="please wait")  # non-actionable
        self.assertFalse(ev.is_reached("g", a.UIObservation((loading,))))

    # -- foreground guard: login can never complete off the target app ------ #
    def test_not_reached_while_target_app_not_foreground(self):
        # Live bug: the store window (Google Play) is still foreground after the
        # Open tap, yet its UI has actionable elements and no sign-in blocker.
        # With a foreground check reporting "not the target app", login must NOT
        # complete - regardless of the (store) UI observed.
        ev = a.SignedInCopilotGoalEvaluator(foreground_check=lambda: False)
        store_ui = _ui_el(text="Open", resource_id="play_open", clickable=True)
        self.assertFalse(ev.is_reached("g", a.UIObservation((store_ui,))))

    def test_reached_when_target_app_foreground_and_inside(self):
        # Same generic "authenticated + inside app" screen, but now the target
        # app IS foreground -> login completes.
        ev = a.SignedInCopilotGoalEvaluator(foreground_check=lambda: True)
        self.assertTrue(
            ev.is_reached("g", a.UIObservation((_post_intro_el(),)))
        )

    def test_foreground_guard_does_not_override_signin_blocker(self):
        # Even when the target app is foreground, a sign-in blocker still means
        # login is not complete (the guard only ADDS a precondition).
        ev = a.SignedInCopilotGoalEvaluator(foreground_check=lambda: True)
        self.assertFalse(ev.is_reached("g", a.UIObservation((_cred_el(),))))

    # -- flow-level: agent STOPS at the boundary ---------------------------- #
    def _flow(self, observations, provider, executor=None):
        # Real login agent (real evaluator + loop + SafetyValidator) with no-op
        # sleep/instant wait so tests incur no real time. Exercises the same
        # boundary logic build_login_agent wires up in production.
        agent = a.AppPilotAgent(
            observer=_SequenceObserver(observations),
            goal_evaluator=a.SignedInCopilotGoalEvaluator(),
            decision_provider=provider,
            safety_validator=a.SafetyValidator(),
            executor=executor or _NoopRecordingExecutor(),
            max_actions=30,
            runtime_context=a.RuntimeContext({}),
            sleep=lambda seconds: None,
            nonactionable_wait_seconds=0,
        )
        return d.SharedLoginFlow(agent)

    def test_already_signed_in_zero_brain_actions(self):
        provider = _RecordingBackProvider()
        executor = _NoopRecordingExecutor()
        flow = self._flow([a.UIObservation((_composer_home_el(),))], provider,
                          executor)
        _capture(flow.ensure_ready)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(executor.executed, 0)

    def test_authenticated_no_onboarding_completes_without_back(self):
        # THE LIVE BUG at flow level: the first (and only) screen is an
        # authenticated app screen with no sign-in blocker and no intro. Login
        # must complete immediately; the Brain must NEVER be consulted (and thus
        # can never choose "press back" to hunt for onboarding).
        provider = _RecordingBackProvider()
        executor = _NoopRecordingExecutor()
        flow = self._flow([a.UIObservation((_post_intro_el(),))], provider,
                          executor)
        _capture(flow.ensure_ready)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(executor.executed, 0)

    def test_already_signed_in_search_screen_zero_brain_actions(self):
        # Already signed in on the Search screen -> immediate completion, no
        # Brain call and no UI action (login never navigates from here).
        provider = _RecordingBackProvider()
        executor = _NoopRecordingExecutor()
        flow = self._flow([a.UIObservation((_search_screen_el(),))], provider,
                          executor)
        _capture(flow.ensure_ready)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(executor.executed, 0)

    def test_shared_login_state_resets_between_runs(self):
        # The SAME shared login flow is reused across runs; per-run state must
        # not leak. Run 1 is already signed in; run 2 (reusing the flow) must
        # still detect sign-in as required rather than inheriting run 1's verdict.
        provider = _RecordingBackProvider()
        executor = _NoopRecordingExecutor()
        observer = _SequenceObserver(
            [
                a.UIObservation((_composer_home_el(),)),  # run 1: already in
                a.UIObservation((_cred_el(),)),           # run 2: sign-in needed
                a.UIObservation((_composer_home_el(),)),  # run 2: signed in
            ]
        )
        agent = a.AppPilotAgent(
            observer=observer,
            goal_evaluator=a.SignedInCopilotGoalEvaluator(),
            decision_provider=provider,
            safety_validator=a.SafetyValidator(),
            executor=executor,
            max_actions=30,
            runtime_context=a.RuntimeContext({}),
            sleep=lambda seconds: None,
            nonactionable_wait_seconds=0,
        )
        flow = d.SharedLoginFlow(agent)

        _, out1 = _capture(flow.ensure_ready)
        self.assertIn("[LOGIN] Already signed in", out1)
        self.assertEqual(provider.calls, 0)

        _, out2 = _capture(flow.ensure_ready)
        self.assertIn("[LOGIN] Sign-in required", out2)
        self.assertIn("[LOGIN] Returning control to deeplink test", out2)
        self.assertGreaterEqual(provider.calls, 1)

    def test_logged_out_takes_authentication_action(self):
        provider = _RecordingBackProvider()
        # credential screen -> then composer (auth done). Brain acts once.
        flow = self._flow(
            [a.UIObservation((_cred_el(),)),
             a.UIObservation((_composer_home_el(),))],
            provider,
        )
        _capture(flow.ensure_ready)
        self.assertGreaterEqual(provider.calls, 1)

    def test_transient_loading_after_auth_waits_without_brain(self):
        # A non-actionable loading screen then the composer: the generic wait
        # handles loading (no Brain, no random Back), then boundary is reached.
        provider = _RecordingBackProvider()
        executor = _NoopRecordingExecutor()
        loading = a.UIObservation((_ui_el(text="please wait"),))  # non-actionable
        flow = self._flow(
            [loading, a.UIObservation((_composer_home_el(),))], provider, executor
        )
        _capture(flow.ensure_ready)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(executor.executed, 0)

    def test_stops_immediately_after_intro_dismissed_no_further_actions(self):
        # intro (dismiss) -> post-intro screen (boundary). Exactly ONE action
        # (the dismissal); the Brain is not consulted on the post-intro screen.
        provider = _RecordingBackProvider()
        executor = _NoopRecordingExecutor()
        flow = self._flow(
            [a.UIObservation((_intro_el(),)),
             a.UIObservation((_post_intro_el(),))],
            provider, executor,
        )
        _capture(flow.ensure_ready)
        self.assertEqual(provider.calls, 1)   # only to dismiss the intro
        self.assertEqual(executor.executed, 1)

    def test_does_not_tap_ask_copilot_after_boundary(self):
        # After the intro is dismissed, an "Ask Copilot" screen appears. The
        # login agent must STOP (boundary reached) and never tap Ask Copilot.
        provider = _RecordingBackProvider()
        executor = _NoopRecordingExecutor()
        flow = self._flow(
            [a.UIObservation((_intro_el(),)),
             a.UIObservation((_ask_copilot_el(),))],
            provider, executor,
        )
        _capture(flow.ensure_ready)
        # Brain asked only for the intro; the Ask Copilot screen is the boundary,
        # so it is never asked to act there (and thus cannot tap Ask Copilot).
        self.assertEqual(provider.calls, 1)
        self.assertEqual(executor.executed, 1)
        # Every observation the Brain saw still contained the intro interruption.
        self.assertTrue(
            all(
                any("get started" in (el.text or "").lower() for el in obs.elements)
                for obs in provider.observations
            )
        )

    def test_installed_and_uninstalled_use_same_login_capability(self):
        # Both paths call ensure_ready() on the SAME shared login instance.
        class _CountingLogin:
            def __init__(self):
                self.calls = 0

            def ensure_ready(self):
                self.calls += 1
                return True  # login preparation succeeded

        login = _CountingLogin()
        judge = _ScriptedJudge([(True, "ok"), (True, "ok")])
        runner, _, _ = _make_runner(
            judge, installer=_FakeInstaller(), login_flow=login
        )
        # Installed batch runs login-if-needed via the orchestrator; the
        # uninstalled case calls the same capability directly.
        with contextlib.redirect_stdout(io.StringIO()):
            runner.ensure_logged_in()
            runner.run_uninstalled_case(_ucase("T1"))
        self.assertEqual(login.calls, 2)


class LLMLoginGoalEvaluatorTests(unittest.TestCase):
    def _ev(self, reached=True, *, actionable_step=False, foreground_check=None,
            transport=None):
        if transport is None:
            def transport(payload):
                self._payload = payload
                self._calls = getattr(self, "_calls", 0) + 1
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "reached": reached,
                                        "actionable_step": actionable_step,
                                        "reason": "r",
                                    }
                                )
                            }
                        }
                    ]
                }
        return a.LLMLoginGoalEvaluator(
            model="m",
            api_key="k",
            base_url="http://x/v1",
            foreground_check=foreground_check,
            transport=transport,
        )

    def _obs(self):
        return a.UIObservation((_ui_el(text="Chat", resource_id="chat", clickable=True),))

    def test_model_says_reached(self):
        self.assertTrue(self._ev(reached=True).is_reached("g", self._obs()))

    def test_model_says_not_reached(self):
        self.assertFalse(self._ev(reached=False).is_reached("g", self._obs()))

    def test_foreground_false_short_circuits_without_model_call(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return {}

        ev = self._ev(
            foreground_check=lambda: False, transport=transport
        )
        self.assertFalse(ev.is_reached("g", self._obs()))
        self.assertFalse(ev.has_actionable_step(self._obs()))
        self.assertEqual(calls, [])

    def test_foreground_true_calls_model(self):
        ev = self._ev(reached=True, foreground_check=lambda: True)
        self.assertTrue(ev.is_reached("g", self._obs()))

    def test_transport_failure_returns_false(self):
        def transport(payload):
            raise RuntimeError("boom")

        self.assertFalse(self._ev(transport=transport).is_reached("g", self._obs()))

    def test_malformed_content_returns_false(self):
        def transport(payload):
            return {"choices": [{"message": {"content": "not json"}}]}

        self.assertFalse(self._ev(transport=transport).is_reached("g", self._obs()))

    def test_prompt_uses_redacted_describe_and_goal(self):
        ev = self._ev(reached=False)
        ev.is_reached("MY-GOAL", self._obs())
        user_msg = self._payload["messages"][1]["content"]
        self.assertIn("MY-GOAL", user_msg)
        self.assertIn("chat", user_msg)

    def test_has_actionable_step_reflects_model(self):
        # A transient loading screen: the model reports no concrete login control.
        self.assertFalse(
            self._ev(reached=False, actionable_step=False)
            .has_actionable_step(self._obs())
        )
        # A real sign-in control present.
        self.assertTrue(
            self._ev(reached=False, actionable_step=True)
            .has_actionable_step(self._obs())
        )

    def test_reached_screen_counts_as_actionable(self):
        # If the model says we are already inside (reached), the wait-gate should
        # not trap us: treat it as actionable (is_reached returns PASS anyway).
        self.assertTrue(
            self._ev(reached=True, actionable_step=False)
            .has_actionable_step(self._obs())
        )

    def test_transport_failure_leaves_step_actionable(self):
        # Fail-open: a judge/transport failure must not trap a real sign-in screen
        # in the wait loop - the Brain can still be asked to drive login.
        def transport(payload):
            raise RuntimeError("boom")

        self.assertTrue(
            self._ev(transport=transport).has_actionable_step(self._obs())
        )

    def test_is_reached_and_step_share_one_model_call(self):
        # The agent calls both on the SAME observation each step; they must reuse
        # a single cached verdict (one transport call), not query the model twice.
        ev = self._ev(reached=True, actionable_step=True)
        obs = self._obs()
        ev.is_reached("g", obs)
        ev.has_actionable_step(obs)
        self.assertEqual(self._calls, 1)

    def test_from_env_requires_model_and_key(self):
        self.assertIsNone(a.LLMLoginGoalEvaluator.from_env({}))
        self.assertIsNone(
            a.LLMLoginGoalEvaluator.from_env({"APPPILOT_MODEL": "m"})
        )
        self.assertIsNone(
            a.LLMLoginGoalEvaluator.from_env({"APPPILOT_MODEL_API_KEY": "k"})
        )

    def test_from_env_builds_when_configured(self):
        ev = a.LLMLoginGoalEvaluator.from_env(
            {
                "APPPILOT_MODEL": "m",
                "APPPILOT_MODEL_API_KEY": "k",
                "APPPILOT_MODEL_BASE_URL": "http://y/v1",
            },
            foreground_check=lambda: True,
        )
        self.assertIsInstance(ev, a.LLMLoginGoalEvaluator)


class LoginWaitsOnTransientScreenTests(unittest.TestCase):
    """End-to-end regression for the live bug: with the AI login evaluator wired
    into the agent, a transient 'looking for accounts' screen (carrying only an
    incidental 'Terms of use' link) must WAIT via the existing mechanism and must
    NOT let the Brain press a diagnostic Back."""

    def _loading_obs(self):
        terms = _ui_el(text="Terms of use", resource_id="terms", clickable=True)
        return a.UIObservation((_ui_el(text="Looking for accounts"), terms))

    def _ev(self, *, reached, actionable_step):
        def transport(payload):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "reached": reached,
                                    "actionable_step": actionable_step,
                                    "reason": "r",
                                }
                            )
                        }
                    }
                ]
            }

        return a.LLMLoginGoalEvaluator(
            model="m", api_key="k", base_url="http://x/v1",
            foreground_check=lambda: True, transport=transport,
        )

    def test_transient_screen_waits_and_never_presses_back(self):
        ev = self._ev(reached=False, actionable_step=False)
        observer = _CountingObserver([self._loading_obs()])
        would_back = _CapturingProvider(
            a.ModelDecision(
                action=a.Action(a.ActionKind.PRESS_BACK),
                reason="No sign-in or onboarding elements visible; pressing back",
            )
        )
        executor = _NoopRecordingExecutor()
        agent = _make_agent(
            observer, would_back, ev, executor=executor,
            max_nonactionable_waits=3,
            actionable_step_check=ev.has_actionable_step,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent.run("login", None)
        self.assertFalse(result)              # bounded, controlled blocked
        self.assertEqual(would_back.calls, 0)      # Brain never consulted
        self.assertEqual(executor.executed, 0)     # so Back never pressed
        self.assertGreater(observer.count, 1)      # it waited and re-observed

    def test_real_control_lets_brain_act(self):
        ev = self._ev(reached=False, actionable_step=True)
        signin = a.UIObservation(
            (_ui_el(text="Continue with Microsoft",
                    resource_id="signin", clickable=True),)
        )
        observer = _CountingObserver([signin])
        provider = _CapturingProvider()  # returns None -> stop after asking
        agent = _make_agent(
            observer, provider, ev,
            actionable_step_check=ev.has_actionable_step,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run("login", None)
        self.assertGreaterEqual(provider.calls, 1)  # Brain consulted to sign in


class DeeplinkSuiteOrchestratorTests(unittest.TestCase):
    def _orch(self, judge, **kwargs):
        runner, executor, sleeps = _make_runner(judge, **kwargs)
        return d.DeeplinkSuiteOrchestrator(runner), executor, sleeps

    def test_prepare_installed_batch_logs_in_then_warms_up(self):
        order = []

        class _OrderLogin:
            def ensure_ready(self_inner):
                order.append("login")
                return True  # login preparation succeeded

        def warm_up():
            order.append("warm_up")

        orch, _, _ = self._orch(
            _ScriptedJudge([]), warm_up=warm_up, login_flow=_OrderLogin()
        )
        orch.prepare_installed_batch()
        # Login-if-needed happens before the single warm-up.
        self.assertEqual(order, ["login", "warm_up"])

    def test_installed_batch_launches_app_before_login(self):
        # Regression: the installed batch only installs the APK (install_fresh);
        # without an explicit launch, login would run against the launcher home
        # and fail. The app MUST be foreground before login, mirroring the
        # uninstalled path's install_and_open.
        order = []

        class _OrderLogin:
            def ensure_ready(self_inner):
                order.append("login")
                return True

        class _OrderInstaller:
            def ensure_absent(self_inner):
                pass

            def install_fresh(self_inner):
                order.append("install")

            def open(self_inner):
                order.append("open")

            def install_and_open(self_inner):
                order.append("install_and_open")

        def warm_up():
            order.append("warm_up")

        orch, _, _ = self._orch(
            _ScriptedJudge([]),
            warm_up=warm_up,
            login_flow=_OrderLogin(),
            installer=_OrderInstaller(),
        )
        orch.prepare_installed_batch()
        # Install -> launch app -> login -> warm-up: login sees the APP, not home.
        self.assertEqual(order, ["install", "open", "login", "warm_up"])

    def test_installed_batch_one_warm_up_before_all_cases(self):
        order = []

        class _OrderLogin:
            def ensure_ready(self_inner):
                order.append("login")
                return True  # login preparation succeeded

        def warm_up():
            order.append("warm_up")

        judge = _ScriptedJudge([(True, "ok")] * 3)
        executor = _RecordingExecutor()
        original_open = executor.open_link

        def open_link(deep_link):
            order.append("open_link")
            original_open(deep_link)

        executor.open_link = open_link
        orch, _, _ = self._orch(
            judge, executor=executor, warm_up=warm_up, login_flow=_OrderLogin()
        )
        report = orch.run([_icase("T1"), _icase("T2"), _icase("T3")])
        self.assertEqual(order.count("warm_up"), 1)  # exactly one, batch-level
        self.assertEqual(order[:2], ["login", "warm_up"])  # before any case
        self.assertEqual(order.count("open_link"), 3)
        self.assertEqual(report.passed, 3)

    def test_uninstalled_never_warms_up_and_uses_shared_login(self):
        warm = _CountingWarmUp()
        login = _FakeLogin()
        installer = _FakeInstaller()
        judge = _ScriptedJudge([(True, "ok")])
        orch, _, _ = self._orch(
            judge, warm_up=warm, login_flow=login, installer=installer
        )
        orch.run([_ucase()])
        self.assertEqual(warm.n, 0)
        self.assertEqual(login.ready_calls, 1)
        self.assertEqual(installer.absent_calls, 2)  # 1 suite-startup + 1 per-attempt

    def test_mixed_shares_one_login_object_and_one_warm_up(self):
        warm = _CountingWarmUp()
        login = _FakeLogin()  # the SAME shared capability object for both paths
        installer = _FakeInstaller()
        judge = _ScriptedJudge([(True, "installed ok"), (True, "fresh ok")])
        orch, _, _ = self._orch(
            judge, warm_up=warm, login_flow=login, installer=installer
        )
        report = orch.run([_icase("T1"), _ucase("T2")])
        self.assertEqual([r.case.test_id for r in report.results], ["T1", "T2"])
        self.assertEqual(warm.n, 1)  # only the installed batch warms up
        # One shared login object invoked by both scenarios (batch + uninstalled).
        self.assertEqual(login.ready_calls, 2)

    def test_orchestrator_matches_runner_run(self):
        # runner.run() delegates to the orchestrator, so results are equivalent.
        judge_a = _ScriptedJudge([(True, "ok"), (False, "no"), (True, "ok")])
        judge_b = _ScriptedJudge([(True, "ok"), (False, "no"), (True, "ok")])
        runner_a, _, _ = _make_runner(judge_a)
        runner_b, _, _ = _make_runner(judge_b)
        cases = [_icase("T1"), _icase("T2")]
        via_runner = runner_a.run(cases).format()
        via_orch = d.DeeplinkSuiteOrchestrator(runner_b).run(cases).format()
        self.assertEqual(via_runner, via_orch)


def _ui_el(*, text="", resource_id="", is_input=False, clickable=False,
           label="", bounds=None, accessibility_text="", hint_text="",
           element_id="e"):
    return a.UIElement(
        element_id=element_id,
        parent_id=None,
        text=text,
        accessibility_text=accessibility_text,
        hint_text=hint_text,
        resource_id=resource_id,
        class_name="",
        clickable=clickable,
        enabled=True,
        is_input=is_input,
        label=label,
        bounds=bounds,
    )


def _logged_out_observation():
    """A realistic signed-out screen: a tappable Sign in control.

    A real logged-out screen exposes actionable UI (e.g. a Sign in button), so
    the generic agent asks the Brain to drive login. An EMPTY observation would
    instead be a non-actionable loading/transition state, which the generic
    agent now (correctly) waits on rather than consulting the Brain."""
    return a.UIObservation(
        (_ui_el(text="Sign in", resource_id="signin_button", clickable=True),)
    )


class _RecordingProvider:
    """A decision provider that records whether the Brain was ever asked."""

    def __init__(self):
        self.calls = 0

    def decide(self, request):
        self.calls += 1
        # Stop immediately without proposing an action; we only need to observe
        # whether the shared login delegated a decision to the Brain.
        return a.ModelDecision(action=None, reason="test: no action")


class _FixedObserver:
    def __init__(self, observation):
        self._observation = observation

    def observe(self):
        return self._observation


class _NoopExecutor:
    def execute(self, *args, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no login action should be executed in these tests")


class SharedLoginOnlyIfNeededTests(unittest.TestCase):
    """The shared login capability reuses the existing SignedInCopilotGoalEvaluator
    to decide whether any login/onboarding is needed - not a new detector."""

    def _login_flow(self, observation, provider):
        import flows.login as login_mod

        agent = login_mod.build_login_agent(
            provider=provider,
            observer=_FixedObserver(observation),
            executor=_NoopExecutor(),
        )
        return d.SharedLoginFlow(agent)

    def test_already_signed_in_takes_no_login_actions(self):
        import contextlib
        import io as _io

        signed_in = a.UIObservation((_ui_el(text="Message Copilot"),))
        provider = _RecordingProvider()
        flow = self._login_flow(signed_in, provider)
        with contextlib.redirect_stdout(_io.StringIO()):
            flow.ensure_ready()
        # SignedInCopilotGoalEvaluator reports ready at step 0: the Brain is never
        # consulted and no login action is taken.
        self.assertEqual(provider.calls, 0)

    def test_logged_out_invokes_shared_login_brain(self):
        import contextlib
        import io as _io

        logged_out = _logged_out_observation()  # not signed in
        provider = _RecordingProvider()
        flow = self._login_flow(logged_out, provider)
        with contextlib.redirect_stdout(_io.StringIO()):
            flow.ensure_ready()
        # Not signed in -> the existing AppPilotAgent + Brain is asked to drive
        # login/onboarding (decision delegated to the model, not hardcoded).
        self.assertGreaterEqual(provider.calls, 1)


# --------------------------------------------------------------------------- #
# Execution-trace logging (observability only; behavior asserted elsewhere)
# --------------------------------------------------------------------------- #
def _capture(fn, *args, **kwargs):
    """Run ``fn`` capturing stdout; return (result, captured_text)."""
    import contextlib
    import io as _io

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class _SequenceObserver:
    """Yields the given observations in order, clamping at the last one."""

    def __init__(self, observations):
        self._obs = list(observations)
        self._i = 0

    def observe(self):
        obs = self._obs[self._i]
        if self._i < len(self._obs) - 1:
            self._i += 1
        return obs


class _BackProvider:
    """Always proposes a safe PRESS_BACK so the agent takes exactly one action."""

    def decide(self, request):
        return a.ModelDecision(
            action=a.Action(a.ActionKind.PRESS_BACK), reason="advance"
        )


class _NoopRecordingExecutor:
    def __init__(self):
        self.executed = 0

    def execute(self, *args, **kwargs):
        self.executed += 1


class TapCommandResolutionTests(unittest.TestCase):
    """The live bug: a Compose 'Continue with Microsoft' button has NO resource
    id and NO own text (its caption lives on a child), so tapping by the merged
    label resolved to the child text node - which reports zero/opaque bounds -
    and the touch landed nowhere, so nothing advanced. Taps must instead land on
    the clickable node itself."""

    def _tap(self, el):
        return a.MaestroExecutor._tap_command(el)

    def test_prefers_resource_id(self):
        el = _ui_el(resource_id="signin_btn", text="Sign in", clickable=True,
                    bounds=(0, 0, 100, 50))
        self.assertEqual(
            self._tap(el), ("flow", '- tapOn:\n    id: "signin_btn"\n')
        )

    def test_uses_own_text_when_no_id(self):
        el = _ui_el(text="Sign in", clickable=True, bounds=(0, 0, 100, 50))
        self.assertEqual(
            self._tap(el), ("flow", '- tapOn:\n    text: "Sign in"\n')
        )

    def test_clickable_without_id_or_own_text_taps_own_center_point(self):
        # Caption lives on a child -> only a merged label, no own text. Must tap
        # the node's own centre as a coordinate, delivered via adb.
        el = _ui_el(label="Continue with Microsoft", clickable=True,
                    bounds=(40, 100, 240, 200))
        self.assertEqual(self._tap(el), ("point", (140, 150)))

    def test_falls_back_to_label_text_when_no_bounds(self):
        el = _ui_el(label="Continue with Microsoft", clickable=True)
        self.assertEqual(
            self._tap(el),
            ("flow", '- tapOn:\n    text: "Continue with Microsoft"\n'),
        )

    def test_no_selector_raises(self):
        with self.assertRaises(ValueError):
            self._tap(_ui_el(clickable=True))

    def test_parse_bounds_reads_uiautomator_format(self):
        parse = a.MaestroHierarchyObserver._parse_bounds
        self.assertEqual(parse("[40,100][240,200]"), (40, 100, 240, 200))
        self.assertIsNone(parse(None))
        self.assertIsNone(parse("garbage"))
        self.assertIsNone(parse("[240,200][40,100]"))  # inverted -> rejected

    def test_element_center(self):
        el = _ui_el(bounds=(10, 20, 30, 60))
        self.assertEqual(el.center, (20, 40))
        self.assertIsNone(_ui_el().center)


class TapDeliveryTests(unittest.TestCase):
    """Coordinate taps must be delivered through adb's input pipeline, not
    Maestro's ``point`` tap - which reports COMPLETED but silently no-ops on
    Compose surfaces so nothing advances."""

    def _executor(self):
        ex = a.MaestroExecutor("com.example.app", "emulator-5554")
        adb_calls = []
        flow_calls = []
        ex._run_adb = lambda args, **kw: adb_calls.append(list(args))
        ex._run_flow = lambda commands, **kw: flow_calls.append(commands)
        return ex, adb_calls, flow_calls

    def test_coordinate_tap_uses_adb_input_tap(self):
        ex, adb_calls, flow_calls = self._executor()
        el = _ui_el(element_id="e:0", label="Continue with Microsoft",
                    clickable=True, bounds=(72, 1992, 1208, 2136))
        obs = a.UIObservation((el,))
        ex.execute(a.Action(a.ActionKind.TAP, target_id="e:0"), obs)
        self.assertEqual(adb_calls, [["shell", "input", "tap", "640", "2064"]])
        self.assertEqual(flow_calls, [])

    def test_text_tap_uses_maestro_flow(self):
        ex, adb_calls, flow_calls = self._executor()
        el = _ui_el(element_id="e:0", text="Sign in", clickable=True,
                    bounds=(0, 0, 100, 50))
        obs = a.UIObservation((el,))
        ex.execute(a.Action(a.ActionKind.TAP, target_id="e:0"), obs)
        self.assertEqual(flow_calls, ['- tapOn:\n    text: "Sign in"\n'])
        self.assertEqual(adb_calls, [])

    def test_selector_tap_falls_back_to_coordinate_when_not_found(self):
        # Regression: a visible element present in our a11y snapshot can be
        # missed by Maestro's own id/text lookup (Compose/loading race). One
        # flaky selector tap must NOT crash the suite - fall back to a coordinate
        # tap on the element's known centre via adb.
        ex, adb_calls, _ = self._executor()
        flow_calls = []

        def failing_flow(commands, **kw):
            flow_calls.append(commands)
            raise RuntimeError(
                "Maestro action execution failed: Element not found: "
                "Id matching regex: nextButton"
            )

        ex._run_flow = failing_flow
        el = _ui_el(element_id="e:0", resource_id="nextButton", clickable=True,
                    bounds=(0, 0, 200, 100))
        obs = a.UIObservation((el,))
        ex.execute(a.Action(a.ActionKind.TAP, target_id="e:0"), obs)
        # Attempted the id selector, then fell back to an adb tap on the centre.
        self.assertEqual(flow_calls, ['- tapOn:\n    id: "nextButton"\n'])
        self.assertEqual(adb_calls, [["shell", "input", "tap", "100", "50"]])

    def test_selector_tap_without_bounds_reraises(self):
        # No bounds -> no safe coordinate fallback, so the failure propagates.
        ex, _, _ = self._executor()

        def failing_flow(commands, **kw):
            raise RuntimeError("Element not found")

        ex._run_flow = failing_flow
        el = _ui_el(element_id="e:0", resource_id="nextButton", clickable=True)
        obs = a.UIObservation((el,))
        with self.assertRaises(RuntimeError):
            ex.execute(a.Action(a.ActionKind.TAP, target_id="e:0"), obs)
        ex, adb_calls, flow_calls = self._executor()
        el = _ui_el(element_id="e:0", label="Field", clickable=True,
                    is_input=True, bounds=(0, 0, 200, 100))
        obs = a.UIObservation((el,))
        ex.execute(
            a.Action(a.ActionKind.INPUT_TEXT, target_id="e:0", input_text="hi"),
            obs,
        )
        self.assertEqual(adb_calls, [["shell", "input", "tap", "100", "50"]])
        self.assertEqual(flow_calls, ['- inputText: "hi"\n'])

    def test_credential_coordinate_input_moves_caret_to_end_before_clearing(self):
        # Regression: eraseText only backspaces LEFT of the caret, so a focus tap
        # landing mid-text left the right portion behind (garbled email field).
        # The caret must be moved to the end (MOVE_END) before the erase.
        ex, adb_calls, flow_calls = self._executor()
        el = _ui_el(element_id="e:0", label="Email", clickable=True,
                    is_input=True, bounds=(0, 0, 200, 100))
        obs = a.UIObservation((el,))
        ex.execute(
            a.Action(
                a.ActionKind.INPUT_TEXT, target_id="e:0",
                credential_kind=a.CredentialKind.USERNAME,
            ),
            obs,
            secret="user@example.com",
        )
        # Focus tap, THEN caret-to-end (KEYCODE_MOVE_END = 123) before erasing.
        self.assertEqual(
            adb_calls,
            [
                ["shell", "input", "tap", "100", "50"],
                ["shell", "input", "keyevent", "123"],
            ],
        )
        # Whole field erased (from the end), then the secret typed via the env
        # placeholder - never the literal value in the flow.
        self.assertEqual(
            flow_calls,
            [
                f"- eraseText: {a.CREDENTIAL_FIELD_ERASE_CHARS}\n",
                f"- inputText: ${{{a.MAESTRO_SECRET_ENV}}}\n",
            ],
        )

    def test_credential_text_input_focuses_then_moves_caret_and_clears(self):
        # Same reliable clear on the text-match (Maestro tapOn) focus path.
        ex, adb_calls, flow_calls = self._executor()
        el = _ui_el(element_id="e:0", text="Email or phone number",
                    clickable=True, is_input=True, bounds=(0, 0, 100, 50))
        obs = a.UIObservation((el,))
        ex.execute(
            a.Action(
                a.ActionKind.INPUT_TEXT, target_id="e:0",
                credential_kind=a.CredentialKind.USERNAME,
            ),
            obs,
            secret="user@example.com",
        )
        # Caret-to-end still runs via adb even when focus was a Maestro tapOn.
        self.assertEqual(adb_calls, [["shell", "input", "keyevent", "123"]])
        self.assertEqual(
            flow_calls,
            [
                '- tapOn:\n    text: "Email or phone number"\n',
                f"- eraseText: {a.CREDENTIAL_FIELD_ERASE_CHARS}\n",
                f"- inputText: ${{{a.MAESTRO_SECRET_ENV}}}\n",
            ],
        )

class KeyboardFilteringTests(unittest.TestCase):
    """The live bug: after typing a password the on-screen keyboard adds 100+
    key nodes that crowd the real sign-in controls (incl. the 'Sign in' button)
    out of the truncated observation, so the login evaluator wrongly concluded
    'reached' before sign-in was submitted. The on-screen keyboard (the active
    IME package) must be excluded from observations, like the system UI."""

    def _observer(self, ime="com.google.android.inputmethod.latin"):
        obs = a.MaestroHierarchyObserver("device", ime_package_provider=lambda: ime)
        obs._ensure_excluded_prefixes()
        return obs

    def _node(self, rid="", text="", clickable=False, children=None):
        attrs = {}
        if rid:
            attrs["resource-id"] = rid
        if text:
            attrs["text"] = text
        if clickable:
            attrs["clickable"] = "true"
        return {"attributes": attrs, "children": children or []}

    def test_keyboard_nodes_excluded_real_controls_remain(self):
        obs = self._observer()
        hierarchy = self._node(children=[
            self._node(rid="app:id/signin", text="Sign in", clickable=True),
            self._node(rid="app:id/pwd", text="Enter password", clickable=True),
            self._node(
                rid="com.google.android.inputmethod.latin:id/key_q",
                text="q", clickable=True,
            ),
            self._node(
                rid="com.google.android.inputmethod.latin:id/key_w",
                text="w", clickable=True,
            ),
        ])
        elements = []
        obs._collect(hierarchy, (), None, False, elements)
        labels = [e.label for e in elements]
        self.assertIn("Sign in", labels)
        self.assertIn("Enter password", labels)
        self.assertNotIn("q", labels)
        self.assertNotIn("w", labels)

    def test_active_ime_package_is_resolved_and_excluded(self):
        obs = self._observer("com.samsung.android.honeyboard")
        self.assertIn("com.samsung.android.honeyboard:", obs._excluded_prefixes)
        self.assertIn("com.android.systemui:", obs._excluded_prefixes)

    def test_ime_lookup_failure_falls_back_to_system_ui_only(self):
        def boom():
            raise RuntimeError("no adb")

        obs = a.MaestroHierarchyObserver("device", ime_package_provider=boom)
        obs._ensure_excluded_prefixes()
        self.assertEqual(obs._excluded_prefixes, ("com.android.systemui:",))

    def test_keyboard_labels_do_not_bubble_into_ancestor_container(self):
        obs = self._observer()
        # A keyboard toolbar container with no id but IME-package children: its
        # label must not inherit the keyboard's text.
        hierarchy = self._node(children=[
            self._node(clickable=True, children=[
                self._node(
                    rid="com.google.android.inputmethod.latin:id/voice",
                    text="Use voice typing", clickable=True,
                ),
            ]),
        ])
        elements = []
        obs._collect(hierarchy, (), None, False, elements)
        joined = " ".join(e.label for e in elements)
        self.assertNotIn("Use voice typing", joined)

    def test_query_ime_package_parses_component(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return types.SimpleNamespace(
                stdout="com.google.android.inputmethod.latin/.LatinIME\n"
            )

        obs = a.MaestroHierarchyObserver("emulator-5554")
        android_module = sys.modules[a.MaestroHierarchyObserver.__module__]
        with mock.patch.object(android_module.subprocess, "run", fake_run):
            self.assertEqual(
                obs._query_ime_package(), "com.google.android.inputmethod.latin"
            )
        self.assertIn("default_input_method", captured["cmd"])


class BlankScreenUnblockTests(unittest.TestCase):
    """A screen whose content is hosted in a separate focused window (e.g. the
    notification opt-in) returns a blank hierarchy dump, so the login agent sees
    ``<no relevant UI elements>`` and stalls. The observer must deterministically
    recover: on a blank observation with a focused pop-up window, press BACK to
    return focus to the app window and re-capture - no AI/OCR/coords/screenshots.
    """

    _BLANK = json.dumps({
        "attributes": {},
        "children": [{"attributes": {"class": "android.view.View"}, "children": []}],
    })
    _CONTENT = json.dumps({
        "attributes": {},
        "children": [{
            "attributes": {"text": "Not now", "clickable": "true"},
            "children": [],
        }],
    })
    _POPUP_FOCUS = "  mCurrentFocus=Window{6a u0 Pop-Up Window}\n"
    _APP_FOCUS = (
        "  mCurrentFocus=Window{6a u0 com.microsoft.office.officehubrow/"
        "com.microsoft.office.officesuite.OfficeSuiteActivity}\n"
    )

    def _observer(self):
        # Provide the IME so _ensure_excluded_prefixes never shells out.
        return a.MaestroHierarchyObserver("device", ime_package_provider=lambda: None)

    def _run_observe(self, obs, *, hierarchies, focus):
        """Drive obs.observe() with scripted maestro/adb subprocess results.

        ``hierarchies`` is consumed one per hierarchy capture; ``focus`` is the
        dumpsys window body. Returns (observation, calls)."""
        calls = []
        pending = list(hierarchies)

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "hierarchy" in cmd:
                return types.SimpleNamespace(returncode=0, stdout=pending.pop(0), stderr="")
            if "dumpsys" in cmd and "window" in cmd:
                return types.SimpleNamespace(returncode=0, stdout=focus, stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        module = sys.modules[a.MaestroHierarchyObserver.__module__]
        with mock.patch.object(module.subprocess, "run", fake_run), \
                mock.patch.object(module.time, "sleep", lambda *_: None):
            observation = obs.observe()
        return observation, calls

    def test_blank_popup_screen_is_unblocked_with_back(self):
        obs = self._observer()
        observation, calls = self._run_observe(
            obs,
            hierarchies=[self._BLANK, self._CONTENT],
            focus=self._POPUP_FOCUS,
        )
        # Recovered: the underlying "Not now" control is now visible.
        self.assertIn("Not now", observation.describe())
        self.assertFalse(a.MaestroHierarchyObserver._is_blank(observation))
        # BACK was delivered exactly once via adb keyevent.
        back_calls = [c for c in calls if "keyevent" in c]
        self.assertEqual(len(back_calls), 1)
        self.assertIn("KEYCODE_BACK", back_calls[0])
        # Two hierarchy captures: the blank one and the recovered one.
        self.assertEqual(sum("hierarchy" in c for c in calls), 2)

    def test_blank_without_popup_focus_does_not_press_back(self):
        obs = self._observer()
        observation, calls = self._run_observe(
            obs,
            hierarchies=[self._BLANK],
            focus=self._APP_FOCUS,
        )
        self.assertTrue(a.MaestroHierarchyObserver._is_blank(observation))
        self.assertEqual([c for c in calls if "keyevent" in c], [])
        self.assertEqual(sum("hierarchy" in c for c in calls), 1)

    def test_unblock_budget_is_bounded(self):
        obs = self._observer()
        obs._popup_unblock_budget = 0
        observation, calls = self._run_observe(
            obs,
            hierarchies=[self._BLANK],
            focus=self._POPUP_FOCUS,
        )
        # Budget exhausted: no dumpsys focus check and no BACK.
        self.assertTrue(a.MaestroHierarchyObserver._is_blank(observation))
        self.assertEqual([c for c in calls if "keyevent" in c], [])
        self.assertEqual(
            [c for c in calls if "dumpsys" in c], []
        )

    def test_non_blank_screen_is_never_touched(self):
        obs = self._observer()
        observation, calls = self._run_observe(
            obs,
            hierarchies=[self._CONTENT],
            focus=self._POPUP_FOCUS,
        )
        self.assertIn("Not now", observation.describe())
        self.assertEqual(sum("hierarchy" in c for c in calls), 1)
        self.assertEqual([c for c in calls if "keyevent" in c], [])

    def test_focused_window_is_popup_parses_current_focus(self):
        obs = self._observer()
        module = sys.modules[a.MaestroHierarchyObserver.__module__]
        with mock.patch.object(
            module.subprocess, "run",
            lambda *a_, **k: types.SimpleNamespace(
                returncode=0, stdout=self._POPUP_FOCUS, stderr=""
            ),
        ):
            self.assertTrue(obs._focused_window_is_popup())
        with mock.patch.object(
            module.subprocess, "run",
            lambda *a_, **k: types.SimpleNamespace(
                returncode=0, stdout=self._APP_FOCUS, stderr=""
            ),
        ):
            self.assertFalse(obs._focused_window_is_popup())


class MaestroDriverStartupRetryTests(unittest.TestCase):
    """The Maestro on-device driver can miss its startup window on a busy
    emulator. That infra flake must be retried a bounded number of times, while
    genuine action failures still raise on the first attempt."""

    def _executor(self):
        return a.MaestroExecutor("com.example.app", "emulator-5554")

    def _module(self):
        return sys.modules[a.MaestroExecutor.__module__]

    def _run_with(self, results):
        """Drive _run_flow with scripted maestro results (one per maestro call).

        adb commands (from the driver-reset recovery) return benign results and
        do not consume the maestro-result queue."""
        pending = list(results)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "maestro" in cmd:
                return pending.pop(0)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        module = self._module()
        with mock.patch.object(module.subprocess, "run", fake_run), \
                mock.patch.object(module.time, "sleep", lambda *_: None):
            self._executor()._run_flow("- pressKey: BACK\n")
        return calls

    _TIMEOUT_ERR = types.SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="Maestro Android driver did not start up in time on emulator",
    )
    _OK = types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_driver_startup_timeout_is_retried_then_succeeds(self):
        calls = self._run_with([self._TIMEOUT_ERR, self._OK])
        # Two maestro invocations: the flaky one and the successful retry.
        self.assertEqual(sum("maestro" in c for c in calls), 2)
        # Recovery reset the adb server before retrying.
        self.assertTrue(any("kill-server" in c for c in calls))
        self.assertTrue(any("start-server" in c for c in calls))

    def test_driver_startup_budget_env_is_passed_to_maestro(self):
        module = self._module()
        captured = {}

        def fake_run(cmd, **kwargs):
            if "maestro" in cmd:
                captured["env"] = kwargs.get("env")
                return self._OK
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(module.subprocess, "run", fake_run):
            self._executor()._run_flow("- pressKey: BACK\n")
        self.assertEqual(
            captured["env"].get(module._DRIVER_STARTUP_TIMEOUT_ENV),
            module._DRIVER_STARTUP_TIMEOUT_MS,
        )

    def test_driver_startup_timeout_exhausts_and_raises(self):
        module = self._module()
        pending = [self._TIMEOUT_ERR] * 5
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "maestro" in cmd:
                return pending.pop(0)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(module.subprocess, "run", fake_run), \
                mock.patch.object(module.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as ctx:
                self._executor()._run_flow("- pressKey: BACK\n")
        self.assertIn("did not start up in time", str(ctx.exception))
        # Bounded: exactly the max number of attempts, no more.
        self.assertEqual(
            sum("maestro" in c for c in calls), module._DRIVER_STARTUP_MAX_ATTEMPTS
        )

    def test_real_action_failure_raises_immediately_without_retry(self):
        module = self._module()
        real_err = types.SimpleNamespace(
            returncode=1, stdout="", stderr="Element not found: Accept"
        )
        pending = [real_err, self._OK]
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "maestro" in cmd:
                return pending.pop(0)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(module.subprocess, "run", fake_run), \
                mock.patch.object(module.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError):
                self._executor()._run_flow("- pressKey: BACK\n")
        # No retry for a genuine failure: only one invocation.
        self.assertEqual(sum("maestro" in c for c in calls), 1)


class ExecutionTraceLoggingTests(unittest.TestCase):
    def test_suite_start_and_completion_logging(self):
        judge = _ScriptedJudge([(True, "installed ok"), (True, "fresh ok")])
        orch, _, _ = d.DeeplinkSuiteOrchestrator(
            _make_runner(
                judge, installer=_FakeInstaller(), login_flow=_FakeLogin()
            )[0]
        ), None, None
        _, out = _capture(orch.run, [_icase("T1"), _ucase("T2")])
        self.assertIn("[SUITE] Starting deeplink test suite", out)
        self.assertIn("[SUITE] Loaded 2 test cases", out)
        self.assertIn("[SUITE] Installed cases: 1", out)
        self.assertIn("[SUITE] Uninstalled cases: 1", out)
        self.assertIn("[INSTALLED BATCH] Starting", out)
        self.assertIn("[INSTALLED BATCH] Ensuring login", out)
        self.assertIn("[SUITE] Completed", out)

    def test_suite_completed_not_logged_when_setup_raises(self):
        class _BoomLogin:
            def ensure_ready(self):
                raise RuntimeError("boom")

        judge = _ScriptedJudge([(True, "ok")])
        runner, _, _ = _make_runner(judge, login_flow=_BoomLogin())
        orch = d.DeeplinkSuiteOrchestrator(runner)
        import contextlib
        import io as _io

        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(RuntimeError):
                orch.run([_icase("T1")])
        # A raised setup failure must NOT print a misleading completion line.
        self.assertNotIn("[SUITE] Completed", buf.getvalue())

    def test_warm_up_cycle_logging_matches_real_cycles(self):
        warm = d.MaestroWarmUp(
            _RecordingExecutor(), launches=2, settle_seconds=3, sleep=lambda s: None
        )
        _, out = _capture(warm)
        self.assertIn("[WARM-UP] Starting installed-app preparation: 2 cycles", out)
        for cycle in (1, 2):
            self.assertIn(f"[WARM-UP] Cycle {cycle}/2: launch app", out)
            self.assertIn(f"[WARM-UP] Cycle {cycle}/2: waiting 3s", out)
            self.assertIn(f"[WARM-UP] Cycle {cycle}/2: stop app", out)
            self.assertIn(f"[WARM-UP] Cycle {cycle}/2 complete", out)
        self.assertIn("[WARM-UP] Installed-app preparation complete", out)

    def test_installed_case_logging(self):
        judge = _ScriptedJudge([(True, "ok")])
        runner, _, _ = _make_runner(judge)
        _, out = _capture(runner.run, [_icase("T1")])
        self.assertIn("[INSTALLED] T1 starting", out)
        self.assertIn("[INSTALLED] T1 attempt 1/2", out)
        self.assertIn("[INSTALLED] T1 opening deeplink", out)
        self.assertIn("[INSTALLED] T1 deeplink test case result: PASS", out)
        # No warm-up log between/around a case when no warm-up is configured.
        self.assertNotIn("[WARM-UP]", out)

    def test_installed_retry_logging(self):
        judge = _ScriptedJudge([(False, "no"), (True, "ok")])
        runner, _, _ = _make_runner(judge)
        _, out = _capture(runner.run, [_icase("T1")])
        self.assertIn("[INSTALLED] T1 attempt 1/2: MISMATCH", out)
        self.assertIn("[INSTALLED] T1 retry: stopping app", out)
        self.assertIn("[INSTALLED] T1 retry: waiting 2s", out)
        self.assertIn("[INSTALLED] T1 retry: reopening same deeplink", out)
        self.assertIn("[INSTALLED] T1 attempt 2/2", out)
        self.assertIn("[INSTALLED] T1 deeplink test case result: PASS", out)

    def test_uninstalled_case_logging(self):
        judge = _ScriptedJudge([(True, "ok")])
        runner, _, _ = _make_runner(
            judge, installer=_FakeInstaller(), login_flow=_FakeLogin()
        )
        _, out = _capture(runner.run, [_ucase("T9")])
        for expected in (
            "[UNINSTALLED] T9 starting",
            "[UNINSTALLED] T9 first-open flow - warm-up not applicable",
            "[UNINSTALLED] T9 attempt 1/2",
            "[UNINSTALLED] T9 ensuring app is uninstalled",
            "[UNINSTALLED] T9 app is uninstalled",
            "[UNINSTALLED] T9 opening deeplink",
            "[INSTALL] T9 installing local build and opening via store button",
            "[INSTALL] T9 app opened",
            "[UNINSTALLED] T9 ensuring login",
            "[UNINSTALLED] T9 login ready",
            "[UNINSTALLED] T9 verifying deeplink expected result",
            "[UNINSTALLED] T9 deeplink test case result: PASS",
        ):
            self.assertIn(expected, out)
        # The uninstalled path must never emit a warm-up trace.
        self.assertNotIn("[WARM-UP]", out)

    def test_uninstalled_retry_logging(self):
        judge = _ScriptedJudge([(False, "no"), (True, "ok")])
        runner, _, _ = _make_runner(
            judge, installer=_FakeInstaller(), login_flow=_FakeLogin()
        )
        _, out = _capture(runner.run, [_ucase("T9")])
        self.assertIn("[UNINSTALLED] T9 attempt 1/2: MISMATCH", out)
        self.assertIn("[UNINSTALLED] T9 retry: recreating fresh-install state", out)
        self.assertIn("[UNINSTALLED] T9 attempt 2/2", out)

    def test_local_apk_internal_launch_logging(self):
        exe = LocalApkInstallerTests._FakeExec()
        installer = d.LocalApkInstaller(
            exe,
            "/tmp/officemobile.apk",
            foreground_poll_seconds=0,
        )
        _, out = _capture(installer.install_and_open)
        self.assertIn("[INSTALL] installing local build", out)
        self.assertIn("[INSTALL] launching app via adb", out)
        # "app opened" is only truthful after the foreground check passes.
        self.assertIn("[INSTALL] waiting for target app to become foreground", out)
        self.assertIn("[INSTALL] target app is foreground", out)

    def test_local_apk_never_foreground_logging(self):
        # When the app never becomes foreground the installer reports the timeout
        # and raises - it must NOT log that the app became foreground.
        exe = LocalApkInstallerTests._FakeExec(foreground_after=10_000)
        installer = d.LocalApkInstaller(
            exe,
            "/tmp/officemobile.apk",
            foreground_timeout_seconds=0,
            foreground_poll_seconds=0,
        )
        with self.assertRaises(RuntimeError):
            _capture(installer.install_and_open)

    def _login_flow(self, observations, provider, executor=None):
        import flows.login as login_mod

        agent = login_mod.build_login_agent(
            provider=provider,
            observer=_SequenceObserver(observations),
            executor=executor or _NoopExecutor(),
        )
        return d.SharedLoginFlow(agent)

    def test_login_already_signed_in_logging(self):
        signed_in = a.UIObservation((_ui_el(text="Message Copilot"),))
        flow = self._login_flow([signed_in], _RecordingProvider())
        _, out = _capture(flow.ensure_ready)
        self.assertIn("[LOGIN] Already signed in - no login actions required", out)
        self.assertNotIn("[LOGIN] Sign-in required", out)
        self.assertNotIn("[LOGIN] Returning control to deeplink test", out)

    def test_login_sign_in_required_logging(self):
        logged_out = _logged_out_observation()
        flow = self._login_flow([logged_out], _RecordingProvider())
        _, out = _capture(flow.ensure_ready)
        self.assertIn("[LOGIN] Sign-in required - starting shared login flow", out)
        self.assertNotIn("[LOGIN] Already signed in", out)

    def test_login_completed_logging(self):
        logged_out = _logged_out_observation()
        signed_in = a.UIObservation((_ui_el(text="Message Copilot"),))
        flow = self._login_flow(
            [logged_out, signed_in], _BackProvider(), executor=_NoopRecordingExecutor()
        )
        _, out = _capture(flow.ensure_ready)
        self.assertIn("[LOGIN] Sign-in required - starting shared login flow", out)
        self.assertIn("[LOGIN] Returning control to deeplink test", out)


# --------------------------------------------------------------------------- #
# PART 1 - generic AppPilot no-actionable-UI wait handling
# --------------------------------------------------------------------------- #
class _CountingObserver:
    """Yields observations in order (clamping at last) and counts observe()."""

    def __init__(self, observations):
        self._obs = list(observations)
        self._i = 0
        self.count = 0

    def observe(self):
        self.count += 1
        obs = self._obs[min(self._i, len(self._obs) - 1)]
        if self._i < len(self._obs) - 1:
            self._i += 1
        return obs


class _CapturingProvider:
    """Records each Brain call and the observation it was asked to decide on."""

    def __init__(self, decision=None):
        self.calls = 0
        self.requests = []
        self._decision = decision

    def decide(self, request):
        self.calls += 1
        self.requests.append(request)
        return self._decision or a.ModelDecision(action=None, reason="test stop")


class _MarkerGoal:
    """Goal reached iff some element's text contains the marker (generic)."""

    def __init__(self, marker):
        self._marker = marker

    def is_reached(self, goal, observation):
        return any(self._marker in (el.text or "") for el in observation.elements)


def _actionable_el(text="Continue"):
    # Clickable + resource id => SafetyValidator yields a TAP (actionable UI).
    return _ui_el(text=text, resource_id="btn_continue", clickable=True)


def _loading_el(text="please wait"):
    # No resource id / not clickable / not input => only PRESS_BACK is available,
    # i.e. NO actionable UI (a transient loading/transition screen).
    return _ui_el(text=text)


def _make_agent(observer, provider, goal, *, executor=None, max_actions=30,
                max_stuck_actions=5, max_nonactionable_waits=10,
                actionable_step_check=None, log_tag=""):
    return a.AppPilotAgent(
        observer=observer,
        goal_evaluator=goal,
        decision_provider=provider,
        safety_validator=a.SafetyValidator(),
        executor=executor or _NoopRecordingExecutor(),
        max_actions=max_actions,
        runtime_context=a.RuntimeContext({}),
        max_stuck_actions=max_stuck_actions,
        sleep=lambda seconds: None,
        nonactionable_wait_seconds=0,
        max_nonactionable_waits=max_nonactionable_waits,
        actionable_step_check=actionable_step_check,
        log_tag=log_tag,
    )


class AgentLogTagTests(unittest.TestCase):
    """Logging-only: the optional ``log_tag`` prefixes EVERY line the agent emits
    with a subsystem tag (e.g. ``[LOGIN]``) so the whole verbose trace is
    greppable and a login PASS cannot be mistaken for the whole deeplink test
    case passing. It must not change verdicts or control flow - only add the
    bracketed prefix."""

    def _reached_agent(self, tag):
        # Goal already reached on first observation => single PASS with no action.
        observer = _CountingObserver([a.UIObservation((_ui_el(text="Message Copilot"),))])
        goal = _MarkerGoal("Message Copilot")
        return _make_agent(observer, _CapturingProvider(), goal, log_tag=tag)

    def _run(self, agent):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = agent.run("reach chat", None)
        return result, buffer.getvalue()

    def test_default_keeps_untagged_lines(self):
        result, out = self._run(self._reached_agent(""))
        self.assertTrue(result)
        self.assertIn("GOAL:\n", out)
        self.assertIn("GOAL REACHED:\ntrue", out)
        self.assertIn("RESULT:\nPASS", out)
        self.assertNotIn("[LOGIN]", out)

    def test_tag_prefixes_every_emitted_line(self):
        result, out = self._run(self._reached_agent("LOGIN"))
        self.assertTrue(result)  # same verdict as the untagged case
        # Every logical entry the agent printed starts with the subsystem tag.
        printed = [ln for ln in out.splitlines() if ln.strip()]
        self.assertTrue(any(ln.startswith("[LOGIN] GOAL:") for ln in printed))
        self.assertIn("[LOGIN] GOAL REACHED:", out)
        self.assertIn("[LOGIN] RESULT:\nPASS", out)
        # No bare, untagged header may leak through for a tagged run.
        self.assertNotIn("\nGOAL REACHED:", "\n" + out)
        self.assertNotIn("\nRESULT:\nPASS", "\n" + out)

    def test_tagged_fail_is_attributed_to_subsystem(self):
        # Never reaches the goal and no actionable step => bounded wait then FAIL.
        observer = _CountingObserver([a.UIObservation((_loading_el(),))])
        goal = _MarkerGoal("never appears")
        agent = _make_agent(
            observer, _CapturingProvider(), goal,
            max_nonactionable_waits=0, log_tag="LOGIN",
        )
        result, out = self._run(agent)
        self.assertFalse(result)
        self.assertIn("[LOGIN] RESULT:\nFAIL -", out)
        self.assertNotIn("\nRESULT:\nFAIL", "\n" + out)

    def test_build_login_agent_tags_output_as_login(self):
        import flows.login as login_mod

        agent = login_mod.build_login_agent(
            "device",
            observer=_CountingObserver(
                [a.UIObservation((_ui_el(text="Message Copilot"),))]
            ),
            executor=_NoopRecordingExecutor(),
            provider=_CapturingProvider(),
        )
        _, out = self._run(agent)
        self.assertIn("[LOGIN] RESULT:\nPASS", out)


class GenericAgentLoadingTests(unittest.TestCase):
    """Part 1: when goal not reached AND no actionable UI, wait/re-observe -
    never ask the Brain to invent an action against a blank/loading screen."""

    def _run(self, agent):
        with contextlib.redirect_stdout(io.StringIO()):
            return agent.run("reach chat", None)

    def test_goal_reached_no_actionable_ui_finishes_without_brain(self):
        # Goal marker present but the only element is non-actionable text.
        obs = a.UIObservation((_loading_el(text="Chat screen with prompt"),))
        provider = _CapturingProvider()
        agent = self._make(obs, provider)
        self.assertTrue(self._run(agent))
        self.assertEqual(provider.calls, 0)

    def test_goal_not_reached_actionable_ui_calls_brain(self):
        obs = a.UIObservation((_actionable_el(),))
        provider = _CapturingProvider()  # returns None -> stop after asking
        agent = self._make(obs, provider)
        self.assertFalse(self._run(agent))
        self.assertGreaterEqual(provider.calls, 1)

    def test_goal_not_reached_no_actionable_ui_does_not_call_brain(self):
        observer = _CountingObserver([a.UIObservation((_loading_el(),))])
        provider = _CapturingProvider()
        agent = self._make_obs(observer, provider, max_nonactionable_waits=3)
        self.assertFalse(self._run(agent))  # bounded, no infinite loop
        self.assertEqual(provider.calls, 0)  # Brain never consulted
        self.assertGreater(observer.count, 1)  # it re-observed while waiting

    def test_loading_then_actionable_calls_brain_with_second_observation(self):
        actionable = a.UIObservation((_actionable_el(text="Sign in"),))
        observer = _CountingObserver(
            [a.UIObservation((_loading_el(),)), actionable]
        )
        provider = _CapturingProvider()
        agent = self._make_obs(observer, provider)
        self.assertFalse(self._run(agent))
        self.assertEqual(provider.calls, 1)
        # The Brain was asked to decide on the SECOND (actionable) observation.
        decided = provider.requests[0].observation
        self.assertTrue(any(el.clickable for el in decided.elements))

    def test_loading_then_goal_finishes_without_brain(self):
        observer = _CountingObserver(
            [
                a.UIObservation((_loading_el(),)),
                a.UIObservation((_loading_el(text="Chat screen with prompt"),)),
            ]
        )
        provider = _CapturingProvider()
        agent = self._make_obs(observer, provider)
        self.assertTrue(self._run(agent))
        self.assertEqual(provider.calls, 0)

    def test_persistent_non_actionable_is_bounded(self):
        observer = _CountingObserver([a.UIObservation((_loading_el(),))])
        provider = _CapturingProvider()
        agent = self._make_obs(observer, provider, max_nonactionable_waits=2)
        # Must return (no hang) and never consult the Brain.
        self.assertFalse(self._run(agent))
        self.assertEqual(provider.calls, 0)
        # 1 initial observe + exactly max_nonactionable_waits re-observes.
        self.assertEqual(observer.count, 3)

    def test_stuck_detection_still_works(self):
        # Unchanging actionable screen + an action every step => stuck FAIL.
        obs = a.UIObservation((_actionable_el(),))
        provider = _BackProvider()
        executor = _NoopRecordingExecutor()
        agent = self._make(obs, provider, executor=executor, max_stuck_actions=2)
        self.assertFalse(self._run(agent))
        self.assertGreaterEqual(executor.executed, 1)

    # -- domain actionable_step gate ----------------------------------------- #
    def test_incidental_control_without_step_waits_instead_of_back(self):
        # The live bug, generically: a transient "looking for accounts" screen
        # with only an incidental clickable "Terms of use" link. Generic
        # classification would consult the Brain, which presses a diagnostic
        # Back. With a domain actionable_step_check reporting NO genuine step, the
        # agent must wait/re-observe instead: never the Brain, never Back.
        terms = _ui_el(text="Terms of use", resource_id="terms", clickable=True)
        loading = a.UIObservation((_ui_el(text="Looking for accounts"), terms))
        observer = _CountingObserver([loading])
        would_back = _CapturingProvider(
            a.ModelDecision(action=a.Action(a.ActionKind.PRESS_BACK), reason="back")
        )
        executor = _NoopRecordingExecutor()
        agent = _make_agent(
            observer, would_back, _MarkerGoal("Chat screen"),
            executor=executor, max_nonactionable_waits=3,
            actionable_step_check=lambda obs: False,
        )
        self.assertFalse(self._run(agent))   # bounded, controlled blocked
        self.assertEqual(would_back.calls, 0)     # Brain never consulted
        self.assertEqual(executor.executed, 0)    # so Back never pressed
        self.assertGreater(observer.count, 1)     # it waited and re-observed

    def test_step_gate_true_consults_brain_normally(self):
        # When the domain gate confirms a genuine step, behaviour is unchanged:
        # the Brain is consulted on the actionable screen.
        obs = a.UIObservation((_actionable_el(text="Sign in"),))
        provider = _CapturingProvider()  # returns None -> stop after asking
        agent = _make_agent(
            _FixedObserver(obs), provider, _MarkerGoal("Chat screen"),
            actionable_step_check=lambda o: True,
        )
        self.assertFalse(self._run(agent))
        self.assertGreaterEqual(provider.calls, 1)

    def test_step_gate_waits_until_real_control_then_consults_brain(self):
        # First the transient screen (gate False) -> wait; then a real sign-in
        # control appears (gate True) -> Brain consulted on the SETTLED screen.
        terms = _ui_el(text="Terms of use", resource_id="terms", clickable=True)
        loading = a.UIObservation((_ui_el(text="Looking for accounts"), terms))
        signin = a.UIObservation((_actionable_el(text="Sign in"),))
        observer = _CountingObserver([loading, signin])
        provider = _CapturingProvider()

        def gate(obs):
            return any("Sign in" in (el.text or "") for el in obs.elements)

        agent = _make_agent(
            observer, provider, _MarkerGoal("Chat screen"),
            actionable_step_check=gate,
        )
        self.assertFalse(self._run(agent))
        self.assertEqual(provider.calls, 1)
        decided = provider.requests[0].observation
        self.assertTrue(any("Sign in" in (el.text or "") for el in decided.elements))

    # -- construction helpers ------------------------------------------------ #
    def _make(self, observation, provider, *, executor=None,
              max_stuck_actions=5):
        return _make_agent(
            _FixedObserver(observation), provider, _MarkerGoal("Chat screen"),
            executor=executor, max_stuck_actions=max_stuck_actions,
        )

    def _make_obs(self, observer, provider, *, max_nonactionable_waits=10):
        return _make_agent(
            observer, provider, _MarkerGoal("Chat screen"),
            max_nonactionable_waits=max_nonactionable_waits,
        )


# --------------------------------------------------------------------------- #
# PART 2 - shared bounded deeplink verification polling (installed == uninstalled)
# --------------------------------------------------------------------------- #
class _FakeClock:
    """Injectable monotonic clock; sleep() advances it (no real waiting)."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _MarkerJudge:
    """Semantic-judge stand-in: match iff observation text contains the marker."""

    def __init__(self, marker):
        self._marker = marker
        self.calls = 0

    def evaluate(self, expected_result, observation):
        self.calls += 1
        matched = any(self._marker in (el.text or "") for el in observation.elements)
        return d.ExpectationVerdict(
            matched=matched, reason="matched" if matched else "not yet"
        )


_LOADING = a.UIObservation((_ui_el(text="loading"),))
_CHAT = a.UIObservation((_ui_el(text="Chat screen with prompt"),))
_MARKER = "Chat screen"


def _make_verify_runner(observer, judge, clock, *, timeout, interval,
                        executor=None, installer=None, login_flow=None,
                        warm_up=None, max_attempts=d.DEFAULT_MAX_ATTEMPTS):
    return d.DeeplinkTestRunner(
        observer=observer,
        executor=executor or _RecordingExecutor(),
        judge=judge,
        warm_up=warm_up,
        sleep=clock.sleep,
        settle_seconds=0,
        verify_timeout_seconds=timeout,
        verify_poll_interval_seconds=interval,
        monotonic=clock.monotonic,
        installer=installer,
        login_flow=login_flow,
        max_attempts=max_attempts,
    )


class DeeplinkVerificationPollingTests(unittest.TestCase):
    def test_immediate_match_no_unnecessary_wait(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        runner = _make_verify_runner(
            _CountingObserver([_CHAT]), judge, clock, timeout=10, interval=2
        )
        result = runner.run_installed_case(_icase("TC001", _MARKER))
        self.assertTrue(result.passed)
        self.assertEqual(judge.calls, 1)  # one observe/judge, no polling
        self.assertEqual(clock.t, 0.0)  # never slept in the verify window

    def test_match_after_several_polls(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        runner = _make_verify_runner(
            _CountingObserver([_LOADING, _LOADING, _CHAT]), judge, clock,
            timeout=10, interval=2,
        )
        result = runner.run_installed_case(_icase("TC002", _MARKER))
        self.assertTrue(result.passed)
        self.assertEqual(judge.calls, 3)
        self.assertEqual(clock.t, 4.0)  # two 2s poll intervals

    def test_match_near_end_of_window(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        runner = _make_verify_runner(
            _CountingObserver([_LOADING, _LOADING, _LOADING, _CHAT]), judge,
            clock, timeout=6, interval=2,
        )
        result = runner.run_installed_case(_icase("TC003", _MARKER))
        self.assertTrue(result.passed)
        self.assertEqual(judge.calls, 4)
        self.assertEqual(clock.t, 6.0)  # matched right at the window edge

    def test_never_matches_times_out_then_retries(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        executor = _RecordingExecutor()
        runner = _make_verify_runner(
            _CountingObserver([_LOADING]), judge, clock, timeout=4, interval=2,
            executor=executor,
        )
        _, out = _capture(runner.run_installed_case, _icase("TC004", _MARKER))
        # A first non-matching observation is NOT a failure; only a mismatch
        # after the bounded window, which drives the existing retry (one retry).
        self.assertIn("[VERIFY] TC004: verification timeout reached", out)
        self.assertIn("[INSTALLED] TC004 attempt 1/2: MISMATCH", out)
        opens = [c for c in executor.calls if c[0] == "open_link"]
        self.assertEqual(len(opens), 2)  # retried the deeplink once

    def test_installed_case_uses_polling(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        runner = _make_verify_runner(
            _CountingObserver([_LOADING, _CHAT]), judge, clock,
            timeout=10, interval=2,
        )
        result = runner.run_installed_case(_icase("TC005", _MARKER))
        self.assertTrue(result.passed)
        self.assertGreater(judge.calls, 1)  # polled, did not fail on first obs

    def test_uninstalled_case_uses_same_polling(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        runner = _make_verify_runner(
            _CountingObserver([_LOADING, _CHAT]), judge, clock,
            timeout=10, interval=2,
            installer=_FakeInstaller(), login_flow=_FakeLogin(),
        )
        result = runner.run_uninstalled_case(_ucase("TC006", _MARKER))
        self.assertTrue(result.passed)
        # SAME shared _verify polling: matched only after re-observing.
        self.assertGreater(judge.calls, 1)


# --------------------------------------------------------------------------- #
# Login -> deeplink handoff: login PREPARES; the deeplink JUDGE owns PASS/FAIL
# --------------------------------------------------------------------------- #
class LoginToDeeplinkHandoffTests(unittest.TestCase):
    """After login completes, the CURRENT screen is handed to the deeplink
    verifier/judge. Login completion is NOT a deeplink pass: the judge - not the
    login evaluator - determines PASS/FAIL from the observed UI."""

    def test_login_completion_hands_current_ui_to_judge_which_passes(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        login = _FakeLogin()
        runner = _make_verify_runner(
            _CountingObserver([_CHAT]), judge, clock, timeout=10, interval=2,
            installer=_FakeInstaller(), login_flow=login,
        )
        result = runner.run_uninstalled_case(_ucase("TC010", _MARKER))
        self.assertTrue(result.passed)
        # Login ran (preparation), THEN the judge decided from the observed UI.
        self.assertEqual(login.ready_calls, 1)
        self.assertGreaterEqual(judge.calls, 1)

    def test_login_completion_does_not_imply_deeplink_pass(self):
        # Login completes every attempt, but the expected result never appears:
        # the judge (not login) owns the verdict, so the case FAILS after the
        # bounded verification window on all 3 attempts.
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)  # never matches _LOADING
        login = _FakeLogin()
        runner = _make_verify_runner(
            _CountingObserver([_LOADING]), judge, clock, timeout=0, interval=0,
            installer=_FakeInstaller(), login_flow=login, max_attempts=3,
        )
        result, _ = _capture(runner.run_uninstalled_case, _ucase("TC011", _MARKER))
        self.assertFalse(result.passed)
        self.assertEqual(login.ready_calls, 3)  # login completed each attempt
        self.assertEqual(judge.calls, 3)        # judge still owned the verdict


class _FailingLogin:
    """Login capability whose preparation FAILS (returns False)."""

    def __init__(self):
        self.ready_calls = 0

    def ensure_ready(self):
        self.ready_calls += 1
        return False  # login preparation failed


class LoginFailurePropagationTests(unittest.TestCase):
    """Login preparation success/failure and deeplink verification are two
    separate states. A login failure must NOT be reported as ready, must NOT
    reach the deeplink judge (_verify), and must fail the test case (retrying
    per the existing scenario-specific path); a login success must hand the
    CURRENT UI straight to the judge without implying a deeplink pass."""

    def test_uninstalled_login_failure_skips_verify_and_is_not_ready(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        login = _FailingLogin()
        installer = _FakeInstaller()
        runner = _make_verify_runner(
            _CountingObserver([_CHAT]), judge, clock, timeout=10, interval=2,
            installer=installer, login_flow=login, max_attempts=1,
        )
        result, out = _capture(runner.run_uninstalled_case, _ucase("TC020", _MARKER))
        self.assertFalse(result.passed)                 # final login failure -> FAIL
        self.assertEqual(judge.calls, 0)                # _verify() never called
        self.assertNotIn("login ready", out)            # never claims ready
        self.assertIn("[UNINSTALLED] TC020 login failed", out)
        self.assertIn("[UNINSTALLED] TC020 skipping deeplink verification", out)
        self.assertNotIn("verifying deeplink expected result", out)

    def test_uninstalled_login_failure_retries_with_fresh_install_state(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        login = _FailingLogin()
        installer = _FakeInstaller()
        runner = _make_verify_runner(
            _CountingObserver([_CHAT]), judge, clock, timeout=10, interval=2,
            installer=installer, login_flow=login, max_attempts=2,
        )
        result, out = _capture(runner.run_uninstalled_case, _ucase("TC021", _MARKER))
        self.assertFalse(result.passed)
        self.assertEqual(login.ready_calls, 2)          # retried once
        self.assertEqual(judge.calls, 0)                # never verified on either attempt
        # The retry recreated genuine fresh-install state (uninstall + install)
        # exactly as the existing scenario-specific path does.
        self.assertEqual(installer.absent_calls, 2)
        self.assertEqual(installer.install_calls, 2)
        self.assertIn("recreating fresh-install state", out)
        self.assertEqual(len(result.attempts), 2)
        self.assertTrue(all(not att.matched for att in result.attempts))

    def test_installed_login_failure_skips_verify_and_fails(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        login = _FailingLogin()
        runner = _make_verify_runner(
            _CountingObserver([_CHAT]), judge, clock, timeout=10, interval=2,
            installer=_FakeInstaller(), login_flow=login,
        )
        orch = d.DeeplinkSuiteOrchestrator(runner)
        report, out = _capture(orch.run, [_icase("TC022", _MARKER)])
        self.assertEqual(report.passed, 0)              # overall test case FAIL
        self.assertEqual(judge.calls, 0)                # _verify() never called
        self.assertNotIn("login ready", out)
        self.assertIn("[INSTALLED] TC022 login failed", out)
        self.assertIn("[INSTALLED] TC022 skipping deeplink verification", out)
        self.assertNotIn("verifying deeplink expected result", out)

    def test_login_success_calls_verify_and_hands_current_ui_to_judge(self):
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        login = _FakeLogin()
        installer = _FakeInstaller()
        runner = _make_verify_runner(
            _CountingObserver([_CHAT]), judge, clock, timeout=10, interval=2,
            installer=installer, login_flow=login, max_attempts=1,
        )
        result, out = _capture(runner.run_uninstalled_case, _ucase("TC023", _MARKER))
        self.assertTrue(result.passed)
        self.assertEqual(login.ready_calls, 1)
        self.assertGreaterEqual(judge.calls, 1)         # _verify() ran on success
        self.assertIn("[UNINSTALLED] TC023 login ready", out)
        self.assertIn("verifying deeplink expected result", out)

    def test_login_success_does_not_imply_deeplink_pass(self):
        # Login succeeds but the CURRENT UI never matches: the judge owns the
        # verdict, so the deeplink test FAILS despite login success.
        clock = _FakeClock()
        judge = _MarkerJudge(_MARKER)
        login = _FakeLogin()
        runner = _make_verify_runner(
            _CountingObserver([_LOADING]), judge, clock, timeout=0, interval=0,
            installer=_FakeInstaller(), login_flow=login, max_attempts=1,
        )
        result, out = _capture(runner.run_uninstalled_case, _ucase("TC024", _MARKER))
        self.assertEqual(login.ready_calls, 1)          # login PASSed
        self.assertFalse(result.passed)                 # deeplink still FAILed
        self.assertIn("[UNINSTALLED] TC024 login ready", out)

    def test_access_restricted_screen_is_valid_login_completion(self):
        # The "Copilot access restricted" dialog is a valid login-completion
        # state: login returns success and the current UI is handed to the judge
        # (which owns the deeplink verdict); login is not failed and no navigation
        # is performed.
        clock = _FakeClock()
        restricted = a.UIObservation(
            (_ui_el(text="You're not eligible to access Copilot"),)
        )
        judge = _MarkerJudge(_MARKER)  # restricted screen won't match the marker
        login = _FakeLogin()           # login completes on the restricted screen
        runner = _make_verify_runner(
            _CountingObserver([restricted]), judge, clock, timeout=0, interval=0,
            installer=_FakeInstaller(), login_flow=login, max_attempts=1,
        )
        result, out = _capture(runner.run_uninstalled_case, _ucase("TC025", _MARKER))
        self.assertEqual(login.ready_calls, 1)          # treated as login completion
        self.assertIn("[UNINSTALLED] TC025 login ready", out)
        self.assertGreaterEqual(judge.calls, 1)         # UI handed to the judge
        self.assertFalse(result.passed)                 # judge owns the FAIL verdict


def _icase_link(test_id, deep_link, expected="Chat screen"):
    """An INSTALLED=True case with an explicit (distinct) deeplink."""
    return d.DeeplinkTestCase(
        test_id=test_id,
        deep_link=deep_link,
        user_type="Premium",
        expected_result=expected,
        installed=True,
    )


class InstalledCaseIsolationTests(unittest.TestCase):
    """Each installed case is process-isolated: batch prep (install+login+warm-up)
    happens exactly once, then every case is triggered by its OWN deeplink (which
    launches the app) and the app is ALWAYS stopped when the case finishes -
    including on failure or an unexpected error - so the next case starts fresh.
    The uninstalled flow is untouched."""

    def test_batch_prep_once_then_each_case_deeplink_then_stop(self):
        judge = _ScriptedJudge([(True, "ok"), (True, "ok")])
        warm = _CountingWarmUp()
        login = _FakeLogin()
        runner, executor, _ = _make_runner(judge, warm_up=warm, login_flow=login)
        ic1 = _icase_link("TC001", "myapp://open/one")
        ic2 = _icase_link("TC002", "myapp://open/two")
        report = runner.run([ic1, ic2])
        # Batch preparation happened exactly once for the whole batch.
        self.assertEqual(warm.n, 1)          # warm-up once, not per case
        self.assertEqual(login.ready_calls, 1)  # login once, not per case
        self.assertEqual(report.passed, 2)
        # Each case: its OWN deeplink launches the app (no explicit launch), then
        # the app is stopped when the case finishes, before the next case.
        self.assertEqual(
            executor.calls,
            [
                ("open_link", "myapp://open/one"),
                ("stop_app", None),
                ("open_link", "myapp://open/two"),
                ("stop_app", None),
            ],
        )

    def test_second_case_starts_with_deeplink_not_explicit_launch(self):
        judge = _ScriptedJudge([(True, "ok"), (True, "ok")])
        runner, executor, _ = _make_runner(judge)
        ic1 = _icase_link("TC001", "myapp://open/one")
        ic2 = _icase_link("TC002", "myapp://open/two")
        runner.run([ic1, ic2])
        # The action that begins TC002 (right after TC001's cleanup stop) is its
        # deeplink - NOT a launch_app/open call.
        idx_second_open = executor.calls.index(("open_link", "myapp://open/two"))
        self.assertEqual(
            executor.calls[idx_second_open - 1], ("stop_app", None)
        )
        self.assertNotIn(("launch_app", None), executor.calls)

    def test_failed_installed_case_still_stops_app(self):
        judge = _ScriptedJudge([(False, "no")])
        runner, executor, _ = _make_runner(judge, max_attempts=1)
        result = runner.run_installed_case(_icase("TCF"))
        self.assertFalse(result.passed)
        self.assertEqual(executor.calls[-1], ("stop_app", None))  # cleanup on FAIL

    def test_installed_case_stops_app_even_when_verify_raises(self):
        class _BoomJudge:
            def evaluate(self, expected_result, observation):
                raise RuntimeError("verify boom")

        runner, executor, _ = _make_runner(_BoomJudge(), max_attempts=1)
        with self.assertRaises(RuntimeError):
            runner.run_installed_case(_icase("TCR"))
        # The finally boundary guarantees cleanup even on an unexpected error.
        self.assertIn(("stop_app", None), executor.calls)
        self.assertEqual(executor.calls[-1], ("stop_app", None))

    def test_uninstalled_case_not_stopped_by_installed_cleanup(self):
        # The installed per-case stop is installed-only: the uninstalled flow's
        # executor calls contain no case-cleanup stop_app (it re-establishes
        # fresh state per attempt instead).
        judge = _ScriptedJudge([(True, "ok")])
        installer = _FakeInstaller()
        login = _FakeLogin()
        runner, executor, _ = _make_runner(
            judge, installer=installer, login_flow=login
        )
        runner.run_uninstalled_case(_ucase("TCU"))
        self.assertNotIn(("stop_app", None), executor.calls)


class _PasswordThenSubmitProvider:
    """Mimics the flaky model that keeps choosing to re-type the password
    whenever the field is offered, and only taps Sign in when it is not. Proves
    the agent withholds an already-entered credential so login advances to
    submit instead of hard-failing on a re-entry loop."""

    def __init__(self):
        self.calls = 0

    def decide(self, request):
        self.calls += 1
        for action in request.available_actions:
            if (
                action.kind == a.ActionKind.INPUT_TEXT
                and action.credential_kind == a.CredentialKind.PASSWORD
            ):
                return a.ModelDecision(action=action, reason="enter password")
        for action in request.available_actions:
            if action.kind == a.ActionKind.TAP:
                element = request.observation.find(action.target_id)
                if element is not None and element.resource_id == "idSIButton9":
                    return a.ModelDecision(action=action, reason="submit")
        return a.ModelDecision(
            action=a.Action(a.ActionKind.PRESS_BACK), reason="fallback"
        )


class _CapturingExecutor:
    def __init__(self):
        self.actions = []

    def execute(self, action, observation, secret=None):
        self.actions.append((action.kind, action.credential_kind))


class EnteredCredentialWithholdingTests(unittest.TestCase):
    def _password_screen(self):
        return a.UIObservation(
            (
                _ui_el(
                    element_id="pw", resource_id="i0118", hint_text="Password",
                    is_input=True, clickable=True, label="Enter password",
                ),
                _ui_el(
                    element_id="signin", resource_id="idSIButton9",
                    text="Sign in", clickable=True, label="Sign in",
                ),
            )
        )

    def _agent(self, observer, provider, executor):
        return a.AppPilotAgent(
            observer=observer,
            goal_evaluator=a.SignedInCopilotGoalEvaluator(),
            decision_provider=provider,
            safety_validator=a.SafetyValidator(),
            executor=executor,
            max_actions=30,
            runtime_context=a.RuntimeContext(
                {a.CredentialKind.PASSWORD: "pw"}
            ),
            sleep=lambda seconds: None,
            nonactionable_wait_seconds=0,
        )

    def test_password_entered_once_then_submit_completes_login(self):
        # Password screen looks identical after entry (the field never echoes),
        # so a naive model re-selects the input. The agent must withhold the
        # entered field and let the model submit - completing login, not looping.
        observer = _SequenceObserver(
            [
                self._password_screen(),
                self._password_screen(),  # identical: field still looks empty
                a.UIObservation((_composer_home_el(),)),  # signed in after submit
            ]
        )
        provider = _PasswordThenSubmitProvider()
        executor = _CapturingExecutor()
        result, out = _capture(
            self._agent(observer, provider, executor).run, "goal", "guidance"
        )
        self.assertTrue(result)
        self.assertNotIn("repeated credential entry", out)
        # Password typed exactly once, then the submit tap - no re-entry loop.
        self.assertEqual(
            executor.actions,
            [
                (a.ActionKind.INPUT_TEXT, a.CredentialKind.PASSWORD),
                (a.ActionKind.TAP, None),
            ],
        )

    def test_credential_offered_again_after_screen_changes(self):
        # Withholding is per-screen: a genuinely new credential screen must offer
        # the input again (the entered-field set is cleared when the UI changes).
        agent = self._agent(_StubObserver(), _RecordingBackProvider(), _CapturingExecutor())
        screen = self._password_screen()
        filled = set()
        # First offer includes the password input.
        actions, fp = agent._offer_actions(screen, filled, None)
        self.assertTrue(
            any(x.credential_kind == a.CredentialKind.PASSWORD for x in actions)
        )
        # Record the field as entered on this screen; it is then withheld.
        filled.add(agent._credential_field_key(
            next(x for x in actions if x.credential_kind == a.CredentialKind.PASSWORD),
            screen,
        ))
        actions, fp = agent._offer_actions(screen, filled, fp)
        self.assertFalse(
            any(x.credential_kind == a.CredentialKind.PASSWORD for x in actions)
        )
        # A genuinely changed screen must clear the memory and offer the input
        # again. Reuse the SAME field id (i0118) but change the surrounding UI,
        # and keep a second actionable element present - so this can only pass
        # because the entered-field memory was cleared on the screen change, not
        # because the stranding guard re-offered a would-be-withheld lone field.
        other = a.UIObservation(
            (
                _ui_el(
                    element_id="pw", resource_id="i0118", hint_text="Password",
                    is_input=True, clickable=True, label="Re-enter password",
                ),
                _ui_el(
                    element_id="verify", resource_id="idSIButton9",
                    text="Verify", clickable=True, label="Verify",
                ),
            )
        )
        actions, _ = agent._offer_actions(other, filled, fp)
        self.assertTrue(
            any(x.credential_kind == a.CredentialKind.PASSWORD for x in actions)
        )

    def test_entered_field_is_still_offered_when_it_is_the_only_action(self):
        # Stranding guard: withholding is skipped when the entered credential is
        # the only actionable (non-Back) element, so the agent is never stuck.
        agent = self._agent(_StubObserver(), _RecordingBackProvider(), _CapturingExecutor())
        lone = a.UIObservation(
            (
                _ui_el(
                    element_id="pw", resource_id="i0118", hint_text="Password",
                    is_input=True, clickable=False, label="Enter password",
                ),
            )
        )
        actions, fp = agent._offer_actions(lone, set(), None)
        password = next(
            x for x in actions if x.credential_kind == a.CredentialKind.PASSWORD
        )
        filled = {agent._credential_field_key(password, lone)}
        # Same screen, field recorded as entered - still offered (no other step).
        actions, _ = agent._offer_actions(lone, filled, fp)
        self.assertTrue(
            any(x.credential_kind == a.CredentialKind.PASSWORD for x in actions)
        )

    def test_withholding_is_specific_to_the_entered_field(self):
        # A second, differently-keyed credential field on the SAME screen is not
        # withheld just because another credential field was entered.
        agent = self._agent(_StubObserver(), _RecordingBackProvider(), _CapturingExecutor())
        two_fields = a.UIObservation(
            (
                _ui_el(
                    element_id="pw", resource_id="i0118", hint_text="Password",
                    is_input=True, clickable=True, label="Enter password",
                ),
                _ui_el(
                    element_id="pin", resource_id="i0119", hint_text="Confirm password",
                    is_input=True, clickable=True, label="Confirm password",
                ),
            )
        )
        actions, fp = agent._offer_actions(two_fields, set(), None)
        first = next(
            x for x in actions
            if x.kind == a.ActionKind.INPUT_TEXT
            and x.credential_kind is not None
            and two_fields.find(x.target_id).resource_id == "i0118"
        )
        filled = {agent._credential_field_key(first, two_fields)}
        actions, _ = agent._offer_actions(two_fields, filled, fp)
        offered_ids = {
            two_fields.find(x.target_id).resource_id
            for x in actions
            if x.kind == a.ActionKind.INPUT_TEXT and x.credential_kind is not None
        }
        self.assertNotIn("i0118", offered_ids)  # entered field withheld
        self.assertIn("i0119", offered_ids)     # other credential field intact


class _FakeRelay:
    """Scripted relay transport recording each POST to /send-report."""

    def __init__(self, status=202):
        self.requests = []
        self._status = status

    def __call__(self, request):
        self.requests.append(request)
        return (self._status, "")


class EmailReportTests(unittest.TestCase):
    _ENV = {
        "APPPILOT_EMAIL_API_URL": "https://relay.example.com/send-report",
        "APPPILOT_EMAIL_API_KEY": "secret-key",
    }

    def _result(self, test_id, passed, *, installed=True,
                expected="Chat screen", reason="restricted screen"):
        case = d.DeeplinkTestCase(
            test_id=test_id,
            deep_link="myapp://open/chat",
            user_type="Premium",
            expected_result=expected,
            installed=installed,
        )
        attempts = [d.AttemptResult(
            attempt=1, matched=passed, reason="match" if passed else reason
        )]
        return d.TestCaseResult(case=case, attempts=attempts)

    def _report(self, *results):
        report = d.SuiteReport()
        report.results.extend(results)
        return report

    def _mixed_report(self):
        return self._report(
            self._result("TC001", True, installed=True),
            self._result("TC002", False, installed=False, reason="age/location block"),
        )

    def test_successful_send(self):
        relay = _FakeRelay()
        report = self._mixed_report()
        result, out = _capture(
            email_report.send_suite_report, report, env=self._ENV, transport=relay
        )
        self.assertTrue(result)
        self.assertIn("[EMAIL] report sent successfully", out)
        # Exactly one authenticated POST to the relay endpoint (single email).
        self.assertEqual(len(relay.requests), 1)
        sent = relay.requests[0]
        self.assertEqual(sent.full_url, self._ENV["APPPILOT_EMAIL_API_URL"])
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(sent.get_header("X-api-key"), "secret-key")
        payload = json.loads(sent.data.decode("utf-8"))
        # With no recipient chosen the caller sends subject + plain body + the
        # HTML table; the sender is fixed server-side and the relay applies its
        # default recipient.
        self.assertEqual(set(payload), {"subject", "body", "html"})
        self.assertIn("FAIL", payload["subject"])
        body = payload["body"]
        self.assertIn("Suite result: FAIL", body)
        self.assertIn("Installed: 1", body)
        self.assertIn("Uninstalled: 1", body)
        self.assertIn("TC001", body)
        self.assertIn("age/location block", body)  # failure reason preserved
        self.assertIn("DEEPLINK TEST REPORT", body)  # existing report is source of truth
        html_body = payload["html"]
        self.assertIn("<table", html_body)
        self.assertIn(">PASS<", html_body)  # green cell for the passing case
        self.assertIn(">FAIL<", html_body)  # red cell for the failing case
        self.assertIn("TC001", html_body)

    def test_only_one_email_for_the_whole_suite(self):
        relay = _FakeRelay()
        report = self._report(
            self._result("TC001", True, installed=True),
            self._result("TC002", True, installed=True),
            self._result("TC003", False, installed=False),
            self._result("TC004", False, installed=False),
        )
        email_report.send_suite_report(report, env=self._ENV, transport=relay)
        self.assertEqual(len(relay.requests), 1)  # one report, not per-case/retry

    def test_missing_configuration_returns_false_without_calling_relay(self):
        env = dict(self._ENV)
        del env["APPPILOT_EMAIL_API_KEY"]
        relay = _FakeRelay()
        result, out = _capture(
            email_report.send_suite_report,
            self._mixed_report(), env=env, transport=relay,
        )
        self.assertFalse(result)
        self.assertIn("missing configuration", out)
        self.assertIn("APPPILOT_EMAIL_API_KEY", out)
        self.assertEqual(relay.requests, [])  # no network attempted

    def test_email_failure_does_not_raise(self):
        # A transport that raises must be swallowed (returns False), so the suite
        # result - computed independently from report.failed - is never affected.
        def boom(_request):
            raise urllib.error.URLError("network down")

        report = self._mixed_report()
        result, out = _capture(
            email_report.send_suite_report, report, env=self._ENV, transport=boom
        )
        self.assertFalse(result)
        self.assertIn("[EMAIL] report send FAILED", out)
        # The report/suite verdict is untouched by the email outcome.
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.passed, 1)

    def test_recipient_is_forwarded_to_the_relay(self):
        relay = _FakeRelay()
        email_report.send_suite_report(
            self._mixed_report(), env=self._ENV,
            recipient="picked@example.com", transport=relay,
        )
        payload = json.loads(relay.requests[0].data.decode("utf-8"))
        self.assertEqual(payload["to"], "picked@example.com")

    def test_no_recipient_omits_to_field(self):
        relay = _FakeRelay()
        email_report.send_suite_report(
            self._mixed_report(), env=self._ENV, transport=relay
        )
        payload = json.loads(relay.requests[0].data.decode("utf-8"))
        self.assertNotIn("to", payload)  # relay uses its fixed default recipient

    def test_html_body_renders_table_with_colored_result_cells(self):
        report = self._mixed_report()
        html_body = email_report.build_html_body(report)
        # A bordered table with a header and one row per case.
        self.assertIn("<table", html_body)
        self.assertIn("S.No", html_body)
        self.assertIn("Result", html_body)
        # Result cells use PASS on a green background / FAIL on a red one.
        self.assertIn("#1e7e34", html_body)  # green (pass)
        self.assertIn("#c62828", html_body)  # red (fail)
        self.assertIn(">PASS<", html_body)
        self.assertIn(">FAIL<", html_body)

    def test_html_body_escapes_dynamic_values(self):
        report = self._report(
            self._result("TC<script>", True, expected="<b>chat</b>"),
        )
        html_body = email_report.build_html_body(report)
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)
        self.assertIn("&lt;b&gt;chat&lt;/b&gt;", html_body)

    def test_html_body_sorts_cases_by_test_id(self):
        # Natural order: TC2 before TC10, regardless of insertion order.
        report = self._report(
            self._result("TC10", True),
            self._result("TC2", True),
            self._result("TC1", True),
        )
        html_body = email_report.build_html_body(report)
        self.assertLess(html_body.index("TC1"), html_body.index("TC2"))
        self.assertLess(html_body.index("TC2"), html_body.index("TC10"))

    def test_html_body_includes_per_attempt_explanations(self):
        report = self._report(
            self._result("TC001", False, installed=True,
                         reason="access restricted by age or location"),
        )
        html_body = email_report.build_html_body(report)
        # Detail row carries the same verdicts + attempt reasons as plain text.
        self.assertIn("Deeplink test:", html_body)
        self.assertIn("Overall test case:", html_body)
        self.assertIn("Attempt 1:", html_body)
        self.assertIn("access restricted by age or location", html_body)
        self.assertIn("mismatch", html_body)

    def test_html_body_forwarded_to_relay(self):
        relay = _FakeRelay()
        email_report.send_suite_report(
            self._mixed_report(), env=self._ENV, transport=relay
        )
        payload = json.loads(relay.requests[0].data.decode("utf-8"))
        self.assertIn("html", payload)
        self.assertIn("<table", payload["html"])

    def test_any_transport_exception_is_swallowed(self):
        # The suite result must be independent of email; realistic failures are
        # NOT urllib.error.URLError (and Ctrl-C is BaseException), so the catch
        # must cover them all.
        for exc in (socket.timeout(), ConnectionResetError(), TimeoutError(),
                    TypeError("bad transport"), ValueError("boom"),
                    KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                def boom(_request, _exc=exc):
                    raise _exc

                result, out = _capture(
                    email_report.send_suite_report,
                    self._mixed_report(), env=self._ENV, transport=boom,
                )
                self.assertFalse(result)
                self.assertIn("[EMAIL] report send FAILED", out)

    def test_success_accepts_any_2xx_status(self):
        # urllib raises HTTPError for non-2xx, so a returned status is always 2xx;
        # 201/204 must count as success, not be rejected.
        for status in (200, 201, 202, 204):
            with self.subTest(status=status):
                relay = _FakeRelay(status=status)
                result = email_report.send_suite_report(
                    self._mixed_report(), env=self._ENV, transport=relay
                )
                self.assertTrue(result)

    def test_3xx_or_error_status_is_treated_as_failure(self):
        for status in (301, 400, 500):
            with self.subTest(status=status):
                relay = _FakeRelay(status=status)
                result, out = _capture(
                    email_report.send_suite_report,
                    self._mixed_report(), env=self._ENV, transport=relay,
                )
                self.assertFalse(result)
                self.assertIn("[EMAIL] report send FAILED", out)

    def test_non_https_relay_url_is_rejected(self):
        relay = _FakeRelay()
        env = dict(self._ENV, APPPILOT_EMAIL_API_URL="http://relay.example.com/x")
        result, out = _capture(
            email_report.send_suite_report,
            self._mixed_report(), env=env, transport=relay,
        )
        self.assertFalse(result)
        self.assertIn("[EMAIL] report send FAILED", out)
        self.assertEqual(relay.requests, [])  # never posted over cleartext

    def test_oversized_body_is_truncated_before_send(self):
        report = self._report(*[
            self._result(f"TC{i:04d}", False, reason="x" * 500)
            for i in range(600)
        ])
        body = email_report.build_body(report)
        self.assertLessEqual(len(body), email_report._MAX_BODY_CHARS + 100)
        self.assertIn("truncated", body)


class EmailRecipientPromptTests(unittest.TestCase):
    """The up-front prompt phase (asked BEFORE the suite runs) is isolated from
    delivery: it only resolves a recipient, never contacts the relay."""

    _ENV = EmailReportTests._ENV

    def _prompt(self, answers, *, interactive=True, saved=None):
        it = iter(answers)
        with mock.patch.object(email_report, "_load_saved_recipient", return_value=saved), \
                mock.patch.object(email_report, "_save_recipient") as save:
            result, out = _capture(
                email_report.prompt_email_recipient,
                env=self._ENV, interactive=interactive,
                input_fn=lambda _prompt: next(it), output=print,
            )
        return result, out, save

    def test_yes_returns_entered_recipient_and_persists_it(self):
        recipient, out, save = self._prompt(["y", "picked@example.com"])
        self.assertEqual(recipient, "picked@example.com")
        self.assertIn("will be emailed to picked@example.com", out)
        save.assert_called_once_with("picked@example.com")

    def test_affirmative_styles_are_accepted(self):
        for answer in ("y", "Y", "yes", "YES", " Yes "):
            with self.subTest(answer=answer):
                recipient, _out, _save = self._prompt([answer, "a@b.com"])
                self.assertEqual(recipient, "a@b.com")

    def test_empty_input_returns_saved_default(self):
        recipient, _out, _save = self._prompt(["y", ""], saved="remembered@x.com")
        self.assertEqual(recipient, "remembered@x.com")

    def test_invalid_email_reprompts_then_returns_valid_one(self):
        recipient, out, _save = self._prompt(["yes", "not-an-email", "good@x.com"])
        self.assertEqual(recipient, "good@x.com")
        self.assertIn("Invalid email address", out)

    def test_gives_up_after_repeated_invalid_addresses(self):
        recipient, out, save = self._prompt(["y", "bad1", "bad2", "bad3"])
        self.assertIsNone(recipient)
        self.assertIn("no valid recipient", out)
        save.assert_not_called()

    def test_decline_returns_none(self):
        recipient, out, save = self._prompt(["n"])
        self.assertIsNone(recipient)
        self.assertIn("declined", out)
        save.assert_not_called()

    def test_cancel_at_first_prompt_returns_none(self):
        def interrupt(_prompt):
            raise KeyboardInterrupt

        recipient, out = _capture(
            email_report.prompt_email_recipient,
            env=self._ENV, interactive=True, input_fn=interrupt,
        )
        self.assertIsNone(recipient)
        self.assertIn("cancelled", out)

    def test_cancel_at_recipient_returns_none_not_saved_default(self):
        # Regression: Ctrl-C at the recipient prompt must ABORT, not silently
        # reuse the remembered address (only empty input accepts the default).
        answers = iter(["y"])

        def ask(_prompt):
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt

        with mock.patch.object(
            email_report, "_load_saved_recipient", return_value="old@example.com"
        ), mock.patch.object(email_report, "_save_recipient") as save:
            recipient, out = _capture(
                email_report.prompt_email_recipient,
                env=self._ENV, interactive=True, input_fn=ask,
            )
        self.assertIsNone(recipient)
        self.assertIn("cancelled", out)
        save.assert_not_called()

    def test_non_interactive_returns_none(self):
        recipient, out, _save = self._prompt(["y", "a@b.com"], interactive=False)
        self.assertIsNone(recipient)
        self.assertIn("non-interactive", out)

    def test_missing_config_returns_none_without_asking(self):
        env = {"APPPILOT_EMAIL_API_URL": "https://relay.example.com/send-report"}
        asked = []

        def record(prompt):
            asked.append(prompt)
            return "y"

        recipient, out = _capture(
            email_report.prompt_email_recipient,
            env=env, interactive=True, input_fn=record,
        )
        self.assertIsNone(recipient)
        self.assertIn("missing configuration", out)
        self.assertEqual(asked, [])

    def test_helper_predicates(self):
        self.assertTrue(email_report.is_affirmative("YES"))
        self.assertFalse(email_report.is_affirmative("nope"))
        self.assertTrue(email_report.is_valid_email("a.b@c.co"))
        self.assertFalse(email_report.is_valid_email("a@b"))

    def test_recipient_round_trips_through_a_real_store_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / ".apppilot_recipient"
            with mock.patch.object(email_report, "_RECIPIENT_STORE", store):
                answers = iter(["y", "saved@example.com"])
                recipient = email_report.prompt_email_recipient(
                    env=self._ENV, interactive=True,
                    input_fn=lambda _p: next(answers),
                )
                self.assertEqual(recipient, "saved@example.com")
                self.assertEqual(store.read_text().strip(), "saved@example.com")
                self.assertEqual(email_report._load_saved_recipient(), "saved@example.com")

    def test_corrupt_store_file_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / ".apppilot_recipient"
            store.write_bytes(b"\xff\xfe not a valid utf8 email \x00")
            with mock.patch.object(email_report, "_RECIPIENT_STORE", store):
                # A corrupt/garbage store must be ignored, not crash the CLI.
                self.assertIsNone(email_report._load_saved_recipient())

    def test_prompt_up_front_then_send_after_delivers_to_chosen_recipient(self):
        # Mirrors the CLI flow: ask BEFORE the run, deliver AFTER it.
        with mock.patch.object(email_report, "_load_saved_recipient", return_value=None), \
                mock.patch.object(email_report, "_save_recipient"):
            answers = iter(["y", "after@example.com"])
            recipient = email_report.prompt_email_recipient(
                env=self._ENV, interactive=True,
                input_fn=lambda _p: next(answers),
            )
        self.assertEqual(recipient, "after@example.com")
        # ... suite would run here ...
        relay = _FakeRelay()
        report = d.SuiteReport()
        report.results.append(d.TestCaseResult(
            case=d.DeeplinkTestCase(
                test_id="TC001", deep_link="myapp://open/chat", user_type="Premium",
                expected_result="Chat screen", installed=True,
            ),
            attempts=[d.AttemptResult(attempt=1, matched=True, reason="match")],
        ))
        sent = email_report.send_suite_report(
            report, env=self._ENV, recipient=recipient, transport=relay,
        )
        self.assertTrue(sent)
        payload = json.loads(relay.requests[0].data.decode("utf-8"))
        self.assertEqual(payload["to"], "after@example.com")


class DeviceCheckTests(unittest.TestCase):
    """The preflight device-check node: deterministic verdict from adb output."""

    def _proc(self, stdout="", returncode=0, stderr=""):
        return types.SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=returncode
        )

    def _runner(self, proc=None, *, raises=None):
        calls = []

        def runner(args):
            calls.append(list(args))
            if raises is not None:
                raise raises
            return proc

        runner.calls = calls
        return runner

    def test_target_device_ready(self):
        runner = self._runner(self._proc(
            "List of devices attached\nemulator-5554\tdevice\n"
        ))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertTrue(result.ok)
        self.assertIn("emulator-5554", result.message)
        self.assertEqual(runner.calls, [["adb", "devices"]])  # read-only query

    def test_no_devices_connected(self):
        runner = self._runner(self._proc("List of devices attached\n\n"))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertFalse(result.ok)
        self.assertIn("no Android device or emulator is connected", result.message)

    def test_unauthorized_device_is_actionable(self):
        runner = self._runner(self._proc(
            "List of devices attached\nemulator-5554\tunauthorized\n"
        ))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertFalse(result.ok)
        self.assertIn("unauthorized", result.message)
        self.assertIn("prompt", result.message)

    def test_offline_device_is_actionable(self):
        runner = self._runner(self._proc(
            "List of devices attached\nemulator-5554\toffline\n"
        ))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertFalse(result.ok)
        self.assertIn("offline", result.message)

    def test_wrong_target_falls_back_to_the_single_ready_device(self):
        # Requested serial absent, exactly one other ready -> auto-use it.
        runner = self._runner(self._proc(
            "List of devices attached\nR58NABCDEF\tdevice\n"
        ))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.device_id, "R58NABCDEF")
        self.assertIn("using the only connected device R58NABCDEF", result.message)
        self.assertTrue(result.any_device_present)

    def test_wrong_target_lists_multiple_ready_devices(self):
        # Several ready, none requested -> ambiguous, do NOT guess.
        runner = self._runner(self._proc(
            "List of devices attached\ndev-a\tdevice\ndev-b\tdevice\n"
        ))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertFalse(result.ok)
        self.assertIsNone(result.device_id)
        self.assertIn("dev-a", result.message)
        self.assertIn("dev-b", result.message)
        self.assertTrue(result.any_device_present)

    def test_ready_target_reports_resolved_id_and_presence(self):
        runner = self._runner(self._proc(
            "List of devices attached\nemulator-5554\tdevice\n"
        ))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertEqual(result.device_id, "emulator-5554")
        self.assertTrue(result.any_device_present)

    def test_no_devices_sets_any_present_false(self):
        # This False signal is what lets the orchestrator try an emulator start.
        runner = self._runner(self._proc("List of devices attached\n\n"))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertFalse(result.ok)
        self.assertFalse(result.any_device_present)

    def test_adb_missing_from_path(self):
        runner = self._runner(raises=FileNotFoundError())
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertFalse(result.ok)
        self.assertIn("adb not found", result.message)
        self.assertIn(
            "https://developer.android.com/tools/releases/platform-tools",
            result.message,
        )

    def test_adb_nonzero_exit_is_reported(self):
        runner = self._runner(self._proc(
            "", returncode=1, stderr="cannot connect to daemon"
        ))
        result = device_check.check_device_ready("emulator-5554", runner)
        self.assertFalse(result.ok)
        self.assertIn("adb devices failed", result.message)
        self.assertIn("cannot connect to daemon", result.message)


class EmulatorAutostartTests(unittest.TestCase):
    """The emulator node: start an existing AVD and wait for boot (all seams faked)."""

    def _proc(self, stdout="", returncode=0, stderr=""):
        return types.SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=returncode
        )

    def test_starts_first_avd_and_returns_booted_serial(self):
        launched = []
        # adb responses: -list-avds, then devices (empty), then devices (new),
        # then boot_completed=1.
        responses = iter([
            self._proc("Pixel_9_Pro\nMedium_Phone_API_36.1\n"),  # -list-avds
            self._proc("List of devices attached\n"),            # before-launch
            self._proc("List of devices attached\nemulator-5554\tdevice\n"),
            self._proc("1\n"),                                    # boot_completed
        ])

        def runner(args):
            return next(responses)

        with mock.patch.object(emulator_node, "emulator_binary",
                               return_value="/sdk/emulator/emulator"):
            result = emulator_node.ensure_emulator_running(
                runner=runner,
                launcher=lambda args: launched.append(list(args)),
                sleeper=lambda _s: None,
                boot_timeout=30, poll_interval=0.01,
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.serial, "emulator-5554")
        # Deterministically starts the FIRST sorted AVD.
        self.assertEqual(
            launched, [["/sdk/emulator/emulator", "-avd", "Medium_Phone_API_36.1"]]
        )

    def test_no_binary_cannot_help(self):
        with mock.patch.object(emulator_node, "emulator_binary",
                               return_value=None):
            result = emulator_node.ensure_emulator_running(
                runner=lambda args: self._proc(""),
                launcher=lambda args: None,
                sleeper=lambda _s: None,
            )
        self.assertFalse(result.ok)
        self.assertIsNone(result.serial)
        self.assertIn("emulator", result.message.lower())

    def test_no_avds_cannot_help(self):
        with mock.patch.object(emulator_node, "emulator_binary",
                               return_value="/sdk/emulator/emulator"):
            result = emulator_node.ensure_emulator_running(
                runner=lambda args: self._proc(""),  # -list-avds -> empty
                launcher=lambda args: None,
                sleeper=lambda _s: None,
            )
        self.assertFalse(result.ok)
        self.assertIn("no emulators (AVDs) are defined", result.message)

    def test_requested_avd_missing_is_reported(self):
        with mock.patch.object(emulator_node, "emulator_binary",
                               return_value="/sdk/emulator/emulator"):
            result = emulator_node.ensure_emulator_running(
                avd="Nonexistent",
                runner=lambda args: self._proc("Pixel_9_Pro\n"),
                launcher=lambda args: None,
                sleeper=lambda _s: None,
            )
        self.assertFalse(result.ok)
        self.assertIn("Nonexistent", result.message)
        self.assertIn("Pixel_9_Pro", result.message)

    def test_boot_timeout_reports_failure(self):
        responses = {
            "-list-avds": self._proc("Pixel_9_Pro\n"),
            "devices": self._proc("List of devices attached\n"),  # never ready
        }

        def runner(args):
            if "-list-avds" in args:
                return responses["-list-avds"]
            return responses["devices"]

        with mock.patch.object(emulator_node, "emulator_binary",
                               return_value="/sdk/emulator/emulator"):
            result = emulator_node.ensure_emulator_running(
                runner=runner,
                launcher=lambda args: None,
                sleeper=lambda _s: None,
                boot_timeout=0.05, poll_interval=0.01,
            )
        self.assertFalse(result.ok)
        self.assertIn("did not become ready", result.message)


class MaestroCheckTests(unittest.TestCase):
    """The Maestro availability node."""

    def _proc(self, stdout="", returncode=0, stderr=""):
        return types.SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=returncode
        )

    def test_maestro_present(self):
        result = maestro_check.check_maestro_ready(
            lambda args: self._proc("1.39.0\n")
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.version, "1.39.0")

    def test_maestro_missing_gives_install_hint(self):
        def runner(args):
            raise FileNotFoundError()

        result = maestro_check.check_maestro_ready(runner)
        self.assertFalse(result.ok)
        self.assertIn("Maestro is not installed", result.message)
        self.assertIn("maestro.mobile.dev", result.message)

    def test_maestro_nonzero_exit(self):
        result = maestro_check.check_maestro_ready(
            lambda args: self._proc("", returncode=1, stderr="boom")
        )
        self.assertFalse(result.ok)
        self.assertIn("maestro --version failed", result.message)


class ModelCheckTests(unittest.TestCase):
    """The evaluation-model configuration node."""

    def test_all_vars_present(self):
        result = model_check.check_model_configured(
            {"APPPILOT_MODEL": "gpt", "APPPILOT_MODEL_API_KEY": "key"}
        )
        self.assertTrue(result.ok)
        self.assertIn("configured", result.message)

    def test_missing_var_reported(self):
        result = model_check.check_model_configured({"APPPILOT_MODEL": "gpt"})
        self.assertFalse(result.ok)
        self.assertIn("APPPILOT_MODEL_API_KEY", result.message)

    def test_empty_var_treated_as_missing(self):
        result = model_check.check_model_configured(
            {"APPPILOT_MODEL": "gpt", "APPPILOT_MODEL_API_KEY": ""}
        )
        self.assertFalse(result.ok)


class CredentialsCheckTests(unittest.TestCase):
    """The sign-in credentials advisory node."""

    def test_both_present(self):
        result = credentials_check.check_credentials_configured(
            {"APPPILOT_USERNAME": "u", "APPPILOT_PASSWORD": "p"}
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.configured)

    def test_missing_reported_but_not_fatal(self):
        result = credentials_check.check_credentials_configured({})
        self.assertTrue(result.ok)
        self.assertFalse(result.configured)
        self.assertIn("APPPILOT_USERNAME", result.message)
        self.assertIn("APPPILOT_PASSWORD", result.message)

    def test_empty_value_treated_as_missing(self):
        result = credentials_check.check_credentials_configured(
            {"APPPILOT_USERNAME": "u", "APPPILOT_PASSWORD": ""}
        )
        self.assertFalse(result.configured)
        self.assertIn("APPPILOT_PASSWORD", result.message)


class PythonCheckTests(unittest.TestCase):
    """The interpreter-version node."""

    def test_current_interpreter_ok(self):
        result = python_check.check_python_version()
        self.assertTrue(result.ok)

    def test_new_enough_boundary(self):
        result = python_check.check_python_version((3, 11, 0))
        self.assertTrue(result.ok)

    def test_too_old_fails(self):
        result = python_check.check_python_version((3, 9, 18))
        self.assertFalse(result.ok)
        self.assertIn("3.11+", result.message)
        self.assertIn("3.9", result.message)


class ConfigStoreTests(unittest.TestCase):
    """The persistent key/value setup store."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "nested" / "config.json"

    def test_set_then_get_roundtrip(self):
        store = ConfigStore(self.path)
        store.set("k", "/some/path")
        self.assertEqual(ConfigStore(self.path).get("k"), "/some/path")

    def test_missing_file_returns_none(self):
        self.assertIsNone(ConfigStore(self.path).get("k"))

    def test_corrupt_file_returns_none(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(ConfigStore(self.path).get("k"))

    def test_non_string_values_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"a": 5, "b": "ok"}', encoding="utf-8")
        store = ConfigStore(self.path)
        self.assertIsNone(store.get("a"))
        self.assertEqual(store.get("b"), "ok")


class PathSetupTests(unittest.TestCase):
    """Resolving a required path via saved -> default -> prompt (+persist)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.store = ConfigStore(self.root / "config.json")
        self.real_dir = self.root / "enlist"
        self.real_dir.mkdir()
        self.real_file = self.root / "cases.xlsx"
        self.real_file.write_text("x", encoding="utf-8")

    def test_saved_value_used_first(self):
        self.store.set("k", str(self.real_dir))
        result = path_setup.resolve_required_path(
            key="k", label="thing", default=None, kind="dir", store=self.store,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.path, str(self.real_dir))

    def test_default_used_when_valid(self):
        result = path_setup.resolve_required_path(
            key="k", label="workbook", default=str(self.real_file),
            kind="file", store=self.store,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.path, str(self.real_file))
        # Defaults are not persisted.
        self.assertIsNone(self.store.get("k"))

    def test_non_interactive_missing_fails(self):
        result = path_setup.resolve_required_path(
            key="k", label="thing", default=str(self.root / "nope"),
            kind="dir", store=self.store, interactive=False,
        )
        self.assertFalse(result.ok)
        self.assertIn("thing", result.message)

    def test_prompt_persists_answer(self):
        replies = iter([str(self.real_dir)])
        result = path_setup.resolve_required_path(
            key="k", label="thing", default=None, kind="dir", store=self.store,
            interactive=True, prompter=lambda _: next(replies),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.path, str(self.real_dir))
        self.assertEqual(self.store.get("k"), str(self.real_dir))

    def test_prompt_retries_until_valid(self):
        replies = iter(["/does/not/exist", str(self.real_file)])
        with contextlib.redirect_stdout(io.StringIO()):
            result = path_setup.resolve_required_path(
                key="k", label="workbook", default=None, kind="file",
                store=self.store, interactive=True,
                prompter=lambda _: next(replies),
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.path, str(self.real_file))

    def test_invalid_saved_falls_back_to_default(self):
        self.store.set("k", str(self.root / "gone"))
        result = path_setup.resolve_required_path(
            key="k", label="workbook", default=str(self.real_file),
            kind="file", store=self.store,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.path, str(self.real_file))

    def test_override_wins_over_saved_and_updates_it(self):
        # A stale saved value must be replaced by an explicitly-provided path.
        old = self.root / "old"
        old.mkdir()
        self.store.set("k", str(old))
        result = path_setup.resolve_required_path(
            key="k", label="thing", default=str(old), override=str(self.real_dir),
            kind="dir", store=self.store,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.path, str(self.real_dir))
        self.assertIn("updated", result.message)
        # The new path is persisted so later runs remember it.
        self.assertEqual(self.store.get("k"), str(self.real_dir))

    def test_invalid_override_fails_without_using_saved(self):
        self.store.set("k", str(self.real_dir))
        result = path_setup.resolve_required_path(
            key="k", label="thing", default=None,
            override=str(self.root / "missing"), kind="dir", store=self.store,
        )
        self.assertFalse(result.ok)
        self.assertIn("missing", result.message)
        # The stale saved value is left untouched (not silently used).
        self.assertEqual(self.store.get("k"), str(self.real_dir))

    def test_keyboard_interrupt_at_prompt_does_not_raise(self):
        def abort(_prompt):
            raise KeyboardInterrupt

        result = path_setup.resolve_required_path(
            key="k", label="thing", default=None, kind="dir", store=self.store,
            interactive=True, prompter=abort,
        )
        self.assertFalse(result.ok)
        self.assertIn("thing", result.message)


class BuildToolsCheckTests(unittest.TestCase):
    """JDK 17 + omrdroid availability node."""

    def _java_home(self):
        # A temp dir with a real bin/java file stands in for a JDK 17 home.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "bin").mkdir()
        (home / "bin" / "java").write_text("", encoding="utf-8")
        return str(home)

    def test_ready_when_jdk_and_omrdroid_present(self):
        result = build_tools_check.check_build_tools(
            java_home=self._java_home(),
            which=lambda name: "/usr/local/bin/omrdroid",
        )
        self.assertTrue(result.ok)
        self.assertIn("Build tools ready", result.message)

    def test_missing_jdk_reported(self):
        result = build_tools_check.check_build_tools(
            java_home="/nonexistent/jdk",
            which=lambda name: "/usr/local/bin/omrdroid",
        )
        self.assertFalse(result.ok)
        self.assertIn("JDK 17 not found", result.message)

    def test_missing_omrdroid_reported(self):
        result = build_tools_check.check_build_tools(
            java_home=self._java_home(),
            which=lambda name: None,
        )
        self.assertFalse(result.ok)
        self.assertIn("omrdroid", result.message)


class ApkSourceTests(unittest.TestCase):
    """APK source selection node (choose once, confirm every run)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = ConfigStore(Path(self._dir.name) / "config.json")

    def test_non_interactive_uses_default_when_nothing_saved(self):
        result = apk_source.select_apk_source(
            store=self.store, default=apk_source.PLAYSTORE, interactive=False
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "playstore")

    def test_non_interactive_prefers_saved_over_default(self):
        self.store.set(apk_source.APK_SOURCE_KEY, apk_source.EXISTING)
        result = apk_source.select_apk_source(
            store=self.store, default=apk_source.BUILD, interactive=False
        )
        self.assertEqual(result.source, "existing")

    def test_number_choice_selected_and_persisted(self):
        result = apk_source.select_apk_source(
            store=self.store, interactive=True,
            prompter=lambda _prompt: "3", output=lambda _line: None,
        )
        self.assertEqual(result.source, "playstore")
        # Persisted so a later run can offer it as the default.
        self.assertEqual(self.store.get(apk_source.APK_SOURCE_KEY), "playstore")

    def test_name_choice_accepted(self):
        result = apk_source.select_apk_source(
            store=self.store, interactive=True,
            prompter=lambda _prompt: "Existing", output=lambda _line: None,
        )
        self.assertEqual(result.source, "existing")

    def test_empty_answer_keeps_current(self):
        self.store.set(apk_source.APK_SOURCE_KEY, apk_source.EXISTING)
        result = apk_source.select_apk_source(
            store=self.store, interactive=True,
            prompter=lambda _prompt: "", output=lambda _line: None,
        )
        self.assertEqual(result.source, "existing")

    def test_invalid_then_valid_reprompts(self):
        answers = iter(["nope", "1"])
        lines = []
        result = apk_source.select_apk_source(
            store=self.store, interactive=True,
            prompter=lambda _prompt: next(answers), output=lines.append,
        )
        self.assertEqual(result.source, "build")
        self.assertTrue(any("1, 2, or 3" in line for line in lines))

    def test_aborted_prompt_keeps_current(self):
        def abort(_prompt):
            raise KeyboardInterrupt

        result = apk_source.select_apk_source(
            store=self.store, default=apk_source.PLAYSTORE, interactive=True,
            prompter=abort, output=lambda _line: None,
        )
        self.assertEqual(result.source, "playstore")


class PreflightTests(unittest.TestCase):
    """The single preflight gate composing every prerequisite check."""

    _ENV = {"APPPILOT_MODEL": "gpt", "APPPILOT_MODEL_API_KEY": "key"}

    def setUp(self):
        # Real, valid default paths + an isolated store so the path-setup steps
        # pass without prompting or touching the operator's real config.
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.store = ConfigStore(root / "config.json")
        self.enlist = root / "enlist"
        self.enlist.mkdir()
        self.cases = root / "cases.xlsx"
        self.cases.write_text("x", encoding="utf-8")

    def _run(self, **overrides):
        kwargs = dict(
            device="emulator-5554",
            env=self._ENV,
            test_cases_default=str(self.cases),
            enlistment_default=str(self.enlist),
            store=self.store,
            maestro_checker=self._ok_maestro(),
            build_tools_checker=lambda: results.CheckResult(True, "Build tools ready."),
            apk_source_selector=self._source(apk_source.BUILD),
            device_checker=self._device_ready(),
        )
        kwargs.update(overrides)
        return preflight_node.run_preflight(**kwargs)

    def _ok_maestro(self):
        return lambda: maestro_check.ToolCheckResult(True, "Maestro 1.0.", "1.0")

    def _source(self, source):
        return lambda **_: apk_source.ApkSourceResult(True, source, f"APK source: {source}.")

    def _device_ready(self, serial="emulator-5554"):
        return lambda requested: device_check.DeviceCheckResult(
            True, f"using device {serial}.", device_id=serial,
            any_device_present=True,
        )

    def test_all_prerequisites_ready(self):
        result = self._run()
        self.assertTrue(result.ok)
        self.assertEqual(result.device_id, "emulator-5554")
        self.assertEqual(result.test_cases_path, str(self.cases))
        self.assertEqual(result.apk_source, "build")
        self.assertEqual(result.enlistment_root, str(self.enlist))
        self.assertIsNone(result.apk_path)

    def test_missing_credentials_warns_but_passes(self):
        # No credential env vars: the run still succeeds (login is a no-op when
        # already signed in), so credentials are advisory, never a hard gate.
        result = self._run(
            credentials_checker=(
                lambda env: credentials_check.CredentialsCheckResult(
                    True, False, "sign-in credentials not set (missing X)."
                )
            )
        )
        self.assertTrue(result.ok)

    def test_test_cases_override_wins_over_saved(self):
        # A saved workbook is overridden (and updated) by an explicit override.
        self.store.set(preflight_node.preflight_check.TEST_CASES_KEY, str(self.cases))
        other = Path(self._dir.name) / "other.xlsx"
        other.write_text("y", encoding="utf-8")
        result = self._run(test_cases_override=str(other))
        self.assertTrue(result.ok)
        self.assertEqual(result.test_cases_path, str(other))
        self.assertEqual(
            self.store.get(preflight_node.preflight_check.TEST_CASES_KEY),
            str(other),
        )

    def test_maestro_missing_fails_first(self):
        # The device checker must NOT be consulted when Maestro is missing.
        def boom(requested):  # pragma: no cover - must never run
            raise AssertionError("device check should not run")

        result = self._run(
            maestro_checker=lambda: maestro_check.ToolCheckResult(
                False, "Maestro is not installed"
            ),
            device_checker=boom,
        )
        self.assertFalse(result.ok)
        self.assertIn("Maestro is not installed", result.message)

    def test_missing_model_config_reported(self):
        result = self._run(env={})
        self.assertFalse(result.ok)
        self.assertIn("no evaluation model configured", result.message)

    def test_missing_build_tools_fails_build_branch(self):
        # In the build branch, build-tools failure short-circuits before device.
        def boom(requested):  # pragma: no cover - must never run
            raise AssertionError("device check should not run")

        result = self._run(
            build_tools_checker=lambda: results.CheckResult(
                False, "JDK 17 not found"
            ),
            device_checker=boom,
        )
        self.assertFalse(result.ok)
        self.assertIn("JDK 17 not found", result.message)

    def test_existing_source_uses_prebuilt_and_skips_build(self):
        # 'existing' resolves a prebuilt APK and needs neither enlistment nor
        # build tools.
        apk = Path(self._dir.name) / "prebuilt.apk"
        apk.write_text("x", encoding="utf-8")
        self.store.set(preflight_node.preflight_check.EXISTING_APK_KEY, str(apk))

        def boom():  # pragma: no cover - build tools must not run
            raise AssertionError("build tools should not run")

        result = self._run(
            apk_source_selector=self._source(apk_source.EXISTING),
            build_tools_checker=boom,
            enlistment_default=str(Path(self._dir.name) / "no-enlist"),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.apk_source, "existing")
        self.assertEqual(result.apk_path, str(apk))
        self.assertIsNone(result.enlistment_root)

    def test_playstore_source_skips_build_and_enlistment(self):
        # 'playstore' installs nothing: no enlistment, no build tools, no APK.
        def boom():  # pragma: no cover - build tools must not run
            raise AssertionError("build tools should not run")

        result = self._run(
            apk_source_selector=self._source(apk_source.PLAYSTORE),
            build_tools_checker=boom,
            enlistment_default=str(Path(self._dir.name) / "no-enlist"),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.apk_source, "playstore")
        self.assertIsNone(result.apk_path)
        self.assertIsNone(result.enlistment_root)

    def test_missing_test_cases_non_interactive_fails(self):
        result = self._run(
            test_cases_default=str(Path(self._dir.name) / "missing.xlsx"),
            interactive=False,
        )
        self.assertFalse(result.ok)
        self.assertIn("test-cases workbook", result.message)

    def test_missing_enlistment_non_interactive_fails(self):
        result = self._run(
            enlistment_default=str(Path(self._dir.name) / "no-enlist"),
            interactive=False,
        )
        self.assertFalse(result.ok)
        self.assertIn("enlistment", result.message)

    def test_no_device_and_no_autostart_fails(self):
        def no_device(requested):
            return device_check.DeviceCheckResult(
                False, "no Android device or emulator is connected.",
                any_device_present=False,
            )

        def boom(**kwargs):  # pragma: no cover - autostart disabled
            raise AssertionError("emulator start should not run")

        result = self._run(
            autostart_emulator=False, device_checker=no_device,
            emulator_starter=boom,
        )
        self.assertFalse(result.ok)
        self.assertIn("no Android device or emulator is connected", result.message)

    def test_no_device_triggers_emulator_autostart(self):
        # First device check finds nothing; after the emulator "starts", the
        # re-check reports it ready. The starter is the injected fake.
        checks = [
            device_check.DeviceCheckResult(
                False, "no Android device or emulator is connected.",
                any_device_present=False,
            ),
            device_check.DeviceCheckResult(
                True, "using device emulator-5554.",
                device_id="emulator-5554", any_device_present=True,
            ),
        ]

        def device_checker(requested):
            return checks.pop(0)

        def starter(**kwargs):
            return emulator_node.EmulatorStartResult(
                True, "emulator-5554", "started emulator."
            )

        result = self._run(device_checker=device_checker, emulator_starter=starter)
        self.assertTrue(result.ok)
        self.assertEqual(result.device_id, "emulator-5554")


if __name__ == "__main__":
    unittest.main()
