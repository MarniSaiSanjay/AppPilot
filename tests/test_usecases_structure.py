"""Regression tests for the Phase-2 shared-node + usecases structure.

These prove the STRUCTURAL migration (not behavior, which the existing suite
covers): facade/import compatibility, the single generic trace helper, the
promoted shared installer/warm-up nodes, and the shared/usecases boundary.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import deeplink_runner as d  # noqa: E402
import shared.installer as installer  # noqa: E402
import shared.warmup as warmup  # noqa: E402
import shared.login as sl  # noqa: E402
import usecases.deeplink as ucd  # noqa: E402
from apppilot import logtags  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"

_PUBLIC_SURFACE = [
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


class FacadeCompatibilityTests(unittest.TestCase):
    def test_deeplink_runner_exposes_full_public_surface(self):
        missing = [name for name in _PUBLIC_SURFACE if not hasattr(d, name)]
        self.assertEqual(missing, [])

    def test_usecases_deeplink_is_authoritative_surface(self):
        # Every facade name resolves to the same object as usecases.deeplink.
        for name in _PUBLIC_SURFACE:
            self.assertIs(getattr(d, name), getattr(ucd, name), name)
        self.assertEqual(sorted(ucd.__all__), sorted(_PUBLIC_SURFACE))

    def test_shared_nodes_reexported_by_facade_are_the_shared_objects(self):
        # The facade re-exports the promoted shared nodes (not use-case copies).
        self.assertIs(d.LocalApkInstaller, installer.LocalApkInstaller)
        self.assertIs(d.AppInstaller, installer.AppInstaller)
        self.assertIs(d.MaestroWarmUp, warmup.MaestroWarmUp)
        self.assertIs(d.WarmUp, warmup.WarmUp)
        self.assertIs(d.SharedLoginFlow, sl.SharedLoginFlow)

    def test_flows_deeplink_shim_reexports_same_objects(self):
        import flows.deeplink as fd

        self.assertIs(fd.DeeplinkTestRunner, ucd.DeeplinkTestRunner)
        self.assertIs(fd.main, ucd.main)


class GenericTraceHelperTests(unittest.TestCase):
    def test_trace_with_tag_matches_prefix_format(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            logtags.trace("hello", logtags.LOGIN)
        self.assertEqual(buf.getvalue(), "[LOGIN] hello\n")

    def test_trace_without_tag_prints_message_only(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            logtags.trace("plain")
        self.assertEqual(buf.getvalue(), "plain\n")

    def test_single_trace_helper_no_duplicates(self):
        # The generic helper is the only _trace-style printer left.
        offenders = [
            p
            for p in SRC.rglob("*.py")
            if "def _trace" in p.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


class SharedWarmUpTests(unittest.TestCase):
    def test_warm_up_runs_launch_settle_stop_cycles(self):
        events = []

        class FakeExecutor:
            def launch_app(self):
                events.append("launch")

            def stop_app(self):
                events.append("stop")

        slept = []
        warm = warmup.MaestroWarmUp(
            FakeExecutor(), launches=2, settle_seconds=1.5, sleep=slept.append
        )
        with contextlib.redirect_stdout(io.StringIO()):
            warm()
        self.assertEqual(events, ["launch", "stop", "launch", "stop"])
        self.assertEqual(slept, [1.5, 1.5])


class SharedInstallerTests(unittest.TestCase):
    def test_open_via_adb_returns_once_foreground(self):
        calls = []

        class FakeExecutor:
            def launch_app_via_adb(self):
                calls.append("adb")

            def is_foreground(self):
                return True

        inst = installer.LocalApkInstaller(
            FakeExecutor(), "/tmp/app.apk", sleep=lambda _s: None
        )
        with contextlib.redirect_stdout(io.StringIO()):
            inst.open()
        self.assertEqual(calls, ["adb"])

    def test_open_via_adb_relaunches_until_foreground(self):
        calls = []
        states = iter([False, True])

        class FakeExecutor:
            def launch_app_via_adb(self):
                calls.append("adb")

            def is_foreground(self):
                return next(states)

        inst = installer.LocalApkInstaller(
            FakeExecutor(), "/tmp/app.apk", sleep=lambda _s: None
        )
        with contextlib.redirect_stdout(io.StringIO()):
            inst.open()
        # Best-effort relaunch while waiting: launch once in open(), once on the
        # first non-foreground poll.
        self.assertEqual(calls, ["adb", "adb"])

    def test_install_fresh_delegates_to_executor(self):
        installed = []

        class FakeExecutor:
            def install_apk(self, path):
                installed.append(path)

        inst = installer.LocalApkInstaller(FakeExecutor(), "/tmp/app.apk")
        with contextlib.redirect_stdout(io.StringIO()):
            inst.install_fresh()
        self.assertEqual(installed, ["/tmp/app.apk"])


class ModuleBoundaryTests(unittest.TestCase):
    def _import_lines(self, path):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                yield stripped

    def test_shared_modules_do_not_import_usecases_or_deeplink(self):
        offenders = []
        for path in (SRC / "shared").rglob("*.py"):
            for stripped in self._import_lines(path):
                if "usecases" in stripped or "flows.deeplink" in stripped:
                    offenders.append(f"{path}: {stripped}")
        self.assertEqual(offenders, [])

    def test_apppilot_modules_do_not_import_usecases_or_flows(self):
        # The required invariant (FINAL CHECK): apppilot never depends on a use
        # case. The one apppilot->shared edge (brain -> shared.model_client, the
        # approved P3-1 delegation) is allowed and proven cycle-free below.
        offenders = []
        for path in (SRC / "apppilot").rglob("*.py"):
            for stripped in self._import_lines(path):
                if "usecases" in stripped or "flows" in stripped:
                    offenders.append(f"{path}: {stripped}")
        self.assertEqual(offenders, [])

    def test_shared_model_client_is_a_leaf(self):
        # model_client must not import apppilot, so brain -> shared.model_client
        # can never form a cycle.
        text = (SRC / "shared" / "model_client.py").read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            if (line.strip().startswith("import ") or line.strip().startswith("from "))
            and ("apppilot" in line or "usecases" in line or "flows" in line)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
