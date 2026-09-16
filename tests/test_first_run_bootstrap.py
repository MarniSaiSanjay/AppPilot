from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


BOOTSTRAP_PATH = Path(__file__).resolve().parents[1] / "src" / "apppilot_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("apppilot_first_run_bootstrap", BOOTSTRAP_PATH)
assert SPEC is not None and SPEC.loader is not None
first_run_bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = first_run_bootstrap
SPEC.loader.exec_module(first_run_bootstrap)


class FirstRunBootstrapTests(unittest.TestCase):
    def test_missing_venv_is_created_only_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            virtualenv = repository / ".venv" / "bin" / "python"
            runner = Mock(side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0),
            ])

            result = first_run_bootstrap.ensure_virtual_environment(
                repository=repository,
                executable="/python",
                interactive=True,
                input_fn=lambda _prompt: "yes",
                runner=runner,
            )

        self.assertEqual(result, virtualenv)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0][:3], ["/python", "-m", "venv"])
        self.assertEqual(runner.call_args_list[1].args[0][1:4], ["-m", "pip", "install"])

    def test_missing_venv_is_not_created_without_confirmation(self) -> None:
        runner = Mock()

        with tempfile.TemporaryDirectory() as temporary:
            result = first_run_bootstrap.ensure_virtual_environment(
                repository=Path(temporary),
                interactive=True,
                input_fn=lambda _prompt: "no",
                output=lambda _message: None,
                runner=runner,
            )

        self.assertIsNone(result)
        runner.assert_not_called()

    def test_incomplete_existing_venv_retries_dependency_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            virtualenv = repository / ".venv" / "bin" / "python"
            virtualenv.parent.mkdir(parents=True)
            virtualenv.touch()
            runner = Mock(side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 1),
                subprocess.CompletedProcess([], 0),
            ])

            result = first_run_bootstrap.ensure_virtual_environment(
                repository=repository,
                interactive=True,
                input_fn=lambda _prompt: "yes",
                runner=runner,
            )

        self.assertEqual(result, virtualenv)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(runner.call_args_list[0].args[0][1], "-c")
        self.assertEqual(runner.call_args_list[1].args[0][1], "-c")
        self.assertEqual(runner.call_args_list[2].args[0][1:4], ["-m", "pip", "install"])

    def test_unsupported_existing_venv_is_recreated_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            virtualenv = repository / ".venv" / "bin" / "python"
            virtualenv.parent.mkdir(parents=True)
            virtualenv.touch()
            runner = Mock(side_effect=[
                subprocess.CompletedProcess([], 1),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0),
            ])

            result = first_run_bootstrap.ensure_virtual_environment(
                repository=repository,
                executable="/python",
                interactive=True,
                input_fn=lambda _prompt: "yes",
                runner=runner,
            )

        self.assertEqual(result, virtualenv)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(runner.call_args_list[1].args[0][:3], ["/python", "-m", "venv"])
        self.assertEqual(runner.call_args_list[2].args[0][1:4], ["-m", "pip", "install"])
