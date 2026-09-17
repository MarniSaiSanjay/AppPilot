from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from apppilot import logtags
from usecases.deeplink import cli


class LogTagTests(unittest.TestCase):
    def tearDown(self) -> None:
        logtags.set_trace_enabled(False)

    def test_trace_is_hidden_by_default(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            logtags.trace("hidden", logtags.SUITE)

        self.assertEqual(output.getvalue(), "")

    def test_trace_is_rendered_when_enabled(self) -> None:
        output = io.StringIO()
        logtags.set_trace_enabled(True)
        with redirect_stdout(output):
            logtags.trace("visible", logtags.SUITE)

        self.assertEqual(output.getvalue(), "[SUITE] visible\n")

    def test_log_flag_enables_detailed_execution_output(self) -> None:
        self.assertTrue(cli._parse_args(["--log"]).log)
