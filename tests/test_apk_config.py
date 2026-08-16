"""Tests for the user-provided local APK path resolution/validation/persistence.

Covers validation rules, the --apk > saved > prompt precedence, saved-path
confirm/replace/stale handling, bounded reprompt (no infinite loop), and
non-interactive behavior. Also asserts the deeplink CLI surface: --rebuild is
gone, --apk exists, and an invalid --apk exits cleanly with code 2."""

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import deeplink_runner as d  # noqa: E402
from apppilot import apk_config  # noqa: E402


def _make_apk(dir_path: Path, name: str = "app.apk") -> Path:
    path = dir_path / name
    path.write_bytes(b"PK\x03\x04 not a real apk, just bytes")
    return path


class _Prompter:
    """Deterministic input_fn returning queued answers; records prompts/output."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.prompts = []
        self.output = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)

    def out(self, message):
        self.output.append(message)


class ApkValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_valid_apk_returns_resolved_path(self):
        apk = _make_apk(self.tmp)
        resolved = apk_config.validate_apk_path(str(apk))
        self.assertEqual(resolved, apk.resolve())
        self.assertTrue(resolved.is_absolute())

    def test_case_insensitive_extension(self):
        apk = _make_apk(self.tmp, "Build.APK")
        self.assertEqual(apk_config.validate_apk_path(str(apk)), apk.resolve())

    def test_expands_user_home(self):
        # A ~-relative path is expanded; use a real file under a fake HOME.
        home = self.tmp / "home"
        home.mkdir()
        apk = _make_apk(home)
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            resolved = apk_config.validate_apk_path("~/app.apk")
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
        self.assertEqual(resolved, apk.resolve())

    def test_empty_path_raises(self):
        with self.assertRaises(ValueError):
            apk_config.validate_apk_path("   ")

    def test_missing_path_raises(self):
        with self.assertRaises(ValueError) as ctx:
            apk_config.validate_apk_path(str(self.tmp / "nope.apk"))
        self.assertIn("does not exist", str(ctx.exception))

    def test_directory_is_not_a_file(self):
        with self.assertRaises(ValueError) as ctx:
            apk_config.validate_apk_path(str(self.tmp))
        self.assertIn("not a file", str(ctx.exception))

    def test_wrong_extension_raises(self):
        other = self.tmp / "app.txt"
        other.write_text("x")
        with self.assertRaises(ValueError) as ctx:
            apk_config.validate_apk_path(str(other))
        self.assertIn(".apk", str(ctx.exception))

    def test_unreadable_path_raises(self):
        apk = _make_apk(self.tmp)
        os.chmod(apk, 0)
        # Root bypasses permission bits; only assert when the bit actually denies.
        if os.access(apk, os.R_OK):
            self.skipTest("cannot make file unreadable in this environment")
        try:
            with self.assertRaises(ValueError) as ctx:
                apk_config.validate_apk_path(str(apk))
            self.assertIn("readable", str(ctx.exception))
        finally:
            os.chmod(apk, stat.S_IRUSR | stat.S_IWUSR)

    def test_structural_validity_is_not_checked(self):
        # The bytes are not a real APK; validation must still pass (adb rejects it).
        apk = _make_apk(self.tmp)
        self.assertEqual(apk_config.validate_apk_path(str(apk)), apk.resolve())


class ApkResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._store = self.tmp / ".apppilot_apk"
        self._orig_store = apk_config._APK_STORE
        apk_config._APK_STORE = self._store

    def tearDown(self):
        apk_config._APK_STORE = self._orig_store

    def _resolve(self, cli_apk=None, *, interactive=True, answers=()):
        p = _Prompter(answers)
        result = apk_config.resolve_apk_path(
            cli_apk, interactive=interactive, input_fn=p.ask, output=p.out,
        )
        return result, p

    def test_cli_apk_takes_precedence_over_saved(self):
        saved = _make_apk(self.tmp, "saved.apk")
        self._store.write_text(str(saved) + "\n")
        chosen = _make_apk(self.tmp, "chosen.apk")
        result, p = self._resolve(str(chosen), interactive=True)
        self.assertEqual(result, chosen.resolve())
        self.assertEqual(p.prompts, [])  # never prompted
        # The explicit path is remembered as the new default.
        self.assertEqual(self._store.read_text().strip(), str(chosen.resolve()))

    def test_invalid_cli_apk_returns_none(self):
        result, p = self._resolve(str(self.tmp / "ghost.apk"), interactive=True)
        self.assertIsNone(result)
        self.assertTrue(any("Invalid APK path" in m for m in p.output))
        self.assertEqual(p.prompts, [])  # authoritative: no fallback prompt

    def test_saved_path_used_on_enter(self):
        # Requirement 1: saved path + Enter -> saved path is used, unchanged.
        saved = _make_apk(self.tmp, "saved.apk")
        self._store.write_text(str(saved) + "\n")
        result, p = self._resolve(interactive=True, answers=[""])
        self.assertEqual(result, saved.resolve())
        # The saved default is shown in brackets, with no Yes/No step.
        self.assertTrue(any(f"[{saved}]" in q for q in p.prompts))
        self.assertEqual(self._store.read_text().strip(), str(saved))  # not modified

    def test_no_yes_no_confirmation_prompt(self):
        # Requirement 5: there is no [Y/n] / "Use this APK?" confirmation step.
        saved = _make_apk(self.tmp, "saved.apk")
        self._store.write_text(str(saved) + "\n")
        _, p = self._resolve(interactive=True, answers=[""])
        blob = " ".join(p.prompts + p.output).lower()
        self.assertNotIn("[y/n]", blob)
        self.assertNotIn("use this apk", blob)

    def test_saved_path_replaced(self):
        # Requirement 2: typing a new valid path uses AND saves it.
        saved = _make_apk(self.tmp, "saved.apk")
        self._store.write_text(str(saved) + "\n")
        new = _make_apk(self.tmp, "new.apk")
        result, p = self._resolve(interactive=True, answers=[str(new)])
        self.assertEqual(result, new.resolve())
        self.assertEqual(self._store.read_text().strip(), str(new.resolve()))
        self.assertTrue(any(f"[{saved}]" in q for q in p.prompts))

    def test_invalid_replacement_does_not_overwrite_saved(self):
        # Requirement 3: an invalid replacement must NOT overwrite the saved path.
        saved = _make_apk(self.tmp, "saved.apk")
        self._store.write_text(str(saved) + "\n")
        missing = str(self.tmp / "missing.apk")
        # Invalid entry -> error (saved untouched); then Enter keeps the saved one.
        result, p = self._resolve(interactive=True, answers=[missing, ""])
        self.assertEqual(result, saved.resolve())
        self.assertEqual(self._store.read_text().strip(), str(saved))
        self.assertTrue(any("Invalid APK path" in m for m in p.output))

    def test_stale_saved_path_falls_through_to_prompt(self):
        self._store.write_text(str(self.tmp / "gone.apk") + "\n")
        new = _make_apk(self.tmp, "fresh.apk")
        # Enter tries the stale saved default, it fails validation, then a new
        # valid path is entered.
        result, p = self._resolve(interactive=True, answers=["", str(new)])
        self.assertEqual(result, new.resolve())
        self.assertTrue(any("no longer valid" in m for m in p.output))

    def test_prompt_when_no_saved_path(self):
        # Requirement 4: no saved path -> plain "APK path:" prompt, entry saved.
        apk = _make_apk(self.tmp)
        result, p = self._resolve(interactive=True, answers=[str(apk)])
        self.assertEqual(result, apk.resolve())
        self.assertEqual(self._store.read_text().strip(), str(apk.resolve()))
        self.assertTrue(any(q.strip() == "APK path:" for q in p.prompts))

    def test_bounded_reprompt_does_not_loop_forever(self):
        bad = str(self.tmp / "missing.apk")
        result, p = self._resolve(interactive=True, answers=[bad, bad, bad, bad])
        self.assertIsNone(result)
        # Exactly max_attempts (default 3) prompts, then give up.
        self.assertEqual(sum(1 for q in p.prompts if "APK path" in q), 3)

    def test_cancel_at_prompt_returns_none(self):
        # No answers queued -> _Prompter raises EOFError -> clean abort.
        result, _ = self._resolve(interactive=True, answers=[])
        self.assertIsNone(result)

    def test_non_interactive_uses_saved_valid_path(self):
        saved = _make_apk(self.tmp, "saved.apk")
        self._store.write_text(str(saved) + "\n")
        result, p = self._resolve(interactive=False)
        self.assertEqual(result, saved.resolve())
        self.assertEqual(p.prompts, [])  # never prompts when non-interactive

    def test_non_interactive_without_saved_returns_none(self):
        result, p = self._resolve(interactive=False)
        self.assertIsNone(result)
        self.assertEqual(p.prompts, [])
        self.assertTrue(any("--apk" in m for m in p.output))

    def test_non_interactive_with_stale_saved_returns_none(self):
        self._store.write_text(str(self.tmp / "gone.apk") + "\n")
        result, p = self._resolve(interactive=False)
        self.assertIsNone(result)
        self.assertEqual(p.prompts, [])


class DeeplinkCliApkSurfaceTests(unittest.TestCase):
    def test_rebuild_flag_is_removed(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                d.main(["--rebuild"])
        self.assertEqual(ctx.exception.code, 2)  # argparse rejects unknown flag

    def test_invalid_apk_exits_2_without_traceback(self):
        # Valid default workbook loads, then an invalid --apk is a clean exit 2
        # (resolution happens before any model/device wiring).
        missing = str(Path(tempfile.mkdtemp()) / "nope.apk")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            rc = d.main(["--apk", missing])
        self.assertEqual(rc, 2)
        self.assertIn("no valid APK path provided", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
