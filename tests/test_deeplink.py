"""Tests for the data-driven AppPilot deeplink test runner.

Covers Excel loading/parsing and required columns, deeplink/expected-result
extraction, the deterministic retry recipe (kill + 2s wait + relaunch), the
3-attempt bound, PASS on later attempts, FAIL after all mismatches, continuing
past failures, expected-failure-state matching, report generation, and the
one-time warm-up.
"""

import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import apppilot_agent as a  # noqa: E402
import deeplink_runner as d  # noqa: E402

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
        cases = d.load_deeplink_cases(REAL_XLSX)
        self.assertEqual([c.test_id for c in cases], ["TC001", "TC002"])
        self.assertTrue(cases[0].deep_link.startswith("https://m365.cloud.microsoft"))
        self.assertEqual(cases[0].expected_result, "Chat screen with prompt")
        self.assertEqual(cases[1].expected_result, "Chat screen")
        self.assertEqual(cases[0].user_type, "Premium")

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
        # No retry -> no stop_app, no wait; a single deeplink launch.
        self.assertEqual(executor.calls, [("open_link", "myapp://open/chat")])
        self.assertEqual(sleeps, [])

    def test_mismatch_triggers_kill_wait_relaunch(self):
        judge = _ScriptedJudge([(False, "wrong screen"), (True, "now correct")])
        runner, executor, sleeps = _make_runner(judge)
        report = runner.run([_case()])
        result = report.results[0]
        self.assertTrue(result.passed)
        self.assertEqual(result.passing_attempt, 2)
        # Exact retry recipe between attempt 1 and 2: kill -> wait 2s -> relaunch.
        self.assertEqual(
            executor.calls,
            [
                ("open_link", "myapp://open/chat"),
                ("stop_app", None),
                ("open_link", "myapp://open/chat"),
            ],
        )
        self.assertEqual(sleeps, [d.DEFAULT_RETRY_WAIT_SECONDS])

    def test_maximum_three_attempts_then_fail(self):
        judge = _ScriptedJudge([(False, "no"), (False, "no"), (False, "no")])
        runner, executor, sleeps = _make_runner(judge)
        report = runner.run([_case()])
        result = report.results[0]
        self.assertFalse(result.passed)
        self.assertEqual(len(result.attempts), 3)
        # Exactly two retries (before attempts 2 and 3): two kills, two waits.
        self.assertEqual(
            [c[0] for c in executor.calls],
            ["open_link", "stop_app", "open_link", "stop_app", "open_link"],
        )
        self.assertEqual(sleeps, [d.DEFAULT_RETRY_WAIT_SECONDS] * 2)

    def test_pass_on_third_attempt(self):
        judge = _ScriptedJudge([(False, "no"), (False, "no"), (True, "yes")])
        runner, _, _ = _make_runner(judge)
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
        runner, _, _ = _make_runner(judge)
        report = runner.run([_case("TC001"), _case("TC002"), _case("TC003")])
        text = report.format()
        self.assertEqual(report.total, 3)
        self.assertEqual(report.passed, 2)
        self.assertEqual(report.failed, 1)
        self.assertIn("TC001  PASS  Attempt 1", text)
        self.assertIn("TC002  PASS  Attempt 2", text)
        self.assertIn("TC003  FAIL  Attempt 3", text)
        self.assertIn("Total: 3", text)
        self.assertIn("Passed: 2", text)
        self.assertIn("Failed: 1", text)
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


if __name__ == "__main__":
    unittest.main()
