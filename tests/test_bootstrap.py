from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apppilot import bootstrap
from apppilot import init


class BootstrapPlanTests(unittest.TestCase):

    def test_java_path_detects_installed_windows_temurin_jdk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            program_files = Path(temporary) / "Program Files"
            java_bin = (
                program_files
                / "Eclipse Adoptium"
                / "jdk-17.0.16.8-hotspot"
                / "bin"
            )
            java_bin.mkdir(parents=True)
            (java_bin / "java.exe").touch()
            env = {"ProgramFiles": str(program_files), "PATH": "/existing"}

            additions = bootstrap.environment.add_java_path(env=env)

        self.assertEqual(additions, (str(java_bin),))
        self.assertEqual(env["PATH"], f"{java_bin}{os.pathsep}/existing")

    def test_git_bash_path_detects_standard_windows_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            program_files = Path(temporary) / "Program Files"
            bash = program_files / "Git" / "bin" / "bash.exe"
            bash.parent.mkdir(parents=True)
            bash.touch()

            detected = bootstrap.environment.git_bash_path(
                env={"ProgramFiles": str(program_files)}
            )

        self.assertEqual(detected, str(bash))

    def test_android_sdk_paths_make_default_tools_available_to_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Android" / "Sdk"
            for relative in (
                "platform-tools",
                "emulator",
                "cmdline-tools/latest/bin",
                "build-tools/36.1.0",
            ):
                (root / relative).mkdir(parents=True, exist_ok=True)
            env = {"LOCALAPPDATA": str(Path(temporary)), "PATH": "/existing"}

            additions = bootstrap.environment.add_android_sdk_paths(env=env)

        self.assertEqual(env["ANDROID_SDK_ROOT"], str(root))
        self.assertEqual(
            additions,
            (
                str(root / "platform-tools"),
                str(root / "emulator"),
                str(root / "cmdline-tools/latest/bin"),
                str(root / "build-tools/36.1.0"),
            ),
        )
        self.assertTrue(env["PATH"].endswith("/existing"))

    def test_macos_plan_installs_missing_tools_but_never_an_avd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            plan = bootstrap.create_plan(
                missing={"java", "maestro", "adb"},
                repository=repository,
                env={"ANDROID_SDK_ROOT": str(repository / "empty-sdk")},
                which=lambda name: {
                    "brew": "/opt/homebrew/bin/brew",
                    "bash": "/bin/sh",
                }.get(name),
                system="Darwin",
            )

        names = [action.name for action in plan.actions]
        self.assertEqual(
            names,
            [
                "java",
                "android-commandline-tools",
                "virtual-environment",
                "python-dependencies",
            ],
        )
        self.assertTrue(
            any(note.startswith("Re-run /setup after Java installs") for note in plan.notes)
        )
        self.assertFalse(any("avd" in name or "image" in name for name in names))

    def test_macos_plan_installs_azure_cli(self) -> None:
        plan = bootstrap.create_plan(
            missing={"az"},
            repository=Path("."),
            which=lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
            system="Darwin",
        )

        self.assertEqual(plan.actions[0].name, "azure-cli")
        self.assertEqual(
            plan.actions[0].command,
            ("/opt/homebrew/bin/brew", "install", "azure-cli"),
        )

    def test_non_macos_plan_explains_that_installation_is_unsupported(self) -> None:
        plan = bootstrap.create_plan(
            missing={"maestro"},
            repository=Path("."),
            system="Linux",
        )

        self.assertEqual(plan.actions, ())
        self.assertIn("macOS and Windows only", plan.notes[0])

    def test_windows_plan_uses_winget_and_never_provisions_an_avd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = bootstrap.create_plan(
                missing={"java", "maestro", "adb"},
                repository=Path(temporary),
                env={"LOCALAPPDATA": str(Path(temporary) / "local")},
                which=lambda name: (
                    "C:\\Windows\\System32\\winget.exe" if name == "winget" else None
                ),
                system="Windows",
            )

        names = [action.name for action in plan.actions]
        self.assertEqual(
            names,
            [
                "java",
                "android-studio",
                "virtual-environment",
                "python-dependencies",
            ],
        )
        self.assertFalse(any("avd" in name or "image" in name for name in names))
        self.assertTrue(
            any(note.startswith("Re-run /setup after Java installs") for note in plan.notes)
        )

    def test_windows_plan_installs_azure_cli(self) -> None:
        plan = bootstrap.create_plan(
            missing={"az"},
            repository=Path("."),
            which=lambda name: (
                "C:\\Windows\\System32\\winget.exe" if name == "winget" else None
            ),
            system="Windows",
        )

        command = plan.actions[0].command
        self.assertEqual(plan.actions[0].name, "azure-cli")
        self.assertIn("Microsoft.AzureCLI", command)
        self.assertIn(
            "After Azure CLI installs, open a new terminal and re-run /setup.",
            plan.notes,
        )

    def test_windows_plan_offers_git_bash_before_maestro(self) -> None:
        plan = bootstrap.create_plan(
            missing={"maestro"},
            repository=Path("."),
            which=lambda name: {
                "winget": "C:\\Windows\\System32\\winget.exe",
                "bash": "C:\\Windows\\System32\\bash.exe",
            }.get(name),
            system="Windows",
        )

        self.assertEqual([action.name for action in plan.actions[:1]], ["git-bash"])
        self.assertIn(
            "--scope",
            next(action.command for action in plan.actions if action.name == "git-bash"),
        )
        self.assertIn(
            "user",
            next(action.command for action in plan.actions if action.name == "git-bash"),
        )
        self.assertTrue(
            any(note.startswith("Re-run /setup after Git Bash") for note in plan.notes)
        )

    def test_windows_plan_uses_git_bash_for_maestro(self) -> None:
        plan = bootstrap.create_plan(
            missing={"maestro"},
            repository=Path("."),
            which=lambda name: {
                "bash": "C:\\Program Files\\Git\\bin\\bash.exe",
            }.get(name),
            system="Windows",
        )

        self.assertEqual([action.name for action in plan.actions[:1]], ["maestro"])

    @patch(
        "apppilot.bootstrap.environment.git_bash_path",
        return_value="C:\\Program Files\\Git\\bin\\bash.exe",
    )
    def test_windows_plan_uses_standard_git_bash_when_wsl_is_on_path(
        self,
        _git_bash_path,
    ) -> None:
        plan = bootstrap.create_plan(
            missing={"maestro"},
            repository=Path("."),
            which=lambda name: (
                "C:\\Windows\\System32\\bash.exe" if name == "bash" else None
            ),
            system="Windows",
        )

        self.assertEqual([action.name for action in plan.actions[:1]], ["maestro"])

    def test_plan_creates_virtual_environment_without_external_missing_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = bootstrap.create_plan(
                missing={"virtual-environment"},
                repository=Path(temporary),
                system="Darwin",
            )

        self.assertEqual(
            [action.name for action in plan.actions],
            ["virtual-environment", "python-dependencies"],
        )

    def test_android_sdk_plan_uses_existing_sdkmanager_without_emulator_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
            manager.parent.mkdir(parents=True)
            manager.touch(mode=0o755)
            plan = bootstrap.create_plan(
                missing={"adb"},
                repository=root / "repo",
                env={"ANDROID_SDK_ROOT": str(root)},
                system="Darwin",
            )

        commands = [action.command for action in plan.actions]
        self.assertIn(
            (
                str(manager),
                f"--sdk_root={root}",
                "platform-tools",
                "emulator",
            ),
            commands,
        )
        self.assertFalse(any("system-images" in command for command in commands))

    def test_windows_plan_recognizes_sdkmanager_batch_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
            manager.parent.mkdir(parents=True)
            manager.touch()
            plan = bootstrap.create_plan(
                missing={"adb"},
                repository=root / "repo",
                env={"ANDROID_SDK_ROOT": str(root)},
                system="Windows",
            )

        self.assertIn(
            (
                str(manager),
                f"--sdk_root={root}",
                "platform-tools",
                "emulator",
            ),
            [action.command for action in plan.actions],
        )

    def test_macos_plan_uses_sdkmanager_found_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "android-commandlinetools"
            manager = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
            manager.parent.mkdir(parents=True)
            manager.touch(mode=0o755)
            link = Path(temporary) / "bin" / "sdkmanager"
            link.parent.mkdir()
            link.symlink_to(manager)
            plan = bootstrap.create_plan(
                missing={"adb"},
                repository=Path(temporary) / "repo",
                env={"ANDROID_SDK_ROOT": str(Path(temporary) / "missing-sdk")},
                which=lambda name: str(link) if name == "sdkmanager" else None,
                system="Darwin",
            )

        self.assertIn(
            (
                str(link),
                f"--sdk_root={root.resolve()}",
                "platform-tools",
                "emulator",
            ),
            [action.command for action in plan.actions],
        )


class BootstrapExecutionTests(unittest.TestCase):
    def test_apply_plan_stops_after_the_first_failure(self) -> None:
        first = bootstrap.BootstrapAction("first", "First", ("first",))
        second = bootstrap.BootstrapAction("second", "Second", ("second",))
        runner = Mock(return_value=subprocess.CompletedProcess([], 1))

        outcomes = bootstrap.apply_plan(bootstrap.BootstrapPlan((first, second)), runner=runner)

        self.assertEqual([(outcome.action.name, outcome.succeeded) for outcome in outcomes],
                         [("first", False)])
        runner.assert_called_once_with(["first"], check=False)

    @patch("apppilot.bootstrap.tempfile.TemporaryDirectory")
    def test_maestro_installer_downloads_then_runs_temporary_file(self, temporary_directory) -> None:
        temporary_directory.return_value.__enter__.return_value = "/tmp/maestro"
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        output: list[str] = []
        action = bootstrap.BootstrapAction(
            "maestro", "Maestro", ("maestro-installer", "/bin/sh")
        )

        outcomes = bootstrap.apply_plan(
            bootstrap.BootstrapPlan((action,)),
            runner=runner,
            output=output.append,
        )

        self.assertTrue(outcomes[0].succeeded)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0][0], "curl")
        self.assertEqual(runner.call_args_list[1].args[0][0], "/bin/sh")
        self.assertEqual(
            output,
            [
                "Maestro installed. AppPilot will use it automatically; no PATH "
                "command is required."
            ],
        )

    @patch("apppilot.bootstrap.Path.home")
    @patch("apppilot.bootstrap.tempfile.TemporaryDirectory")
    def test_maestro_installer_adds_installed_binary_to_path(
        self,
        temporary_directory,
        home,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"PATH": "/existing"}, clear=False
        ):
            maestro_bin = Path(temporary) / ".maestro" / "bin"
            maestro_bin.mkdir(parents=True)
            home.return_value = Path(temporary)
            temporary_directory.return_value.__enter__.return_value = temporary
            runner = Mock(return_value=subprocess.CompletedProcess([], 0))

            bootstrap._install_maestro(runner, "/bin/sh")

            self.assertIn(str(maestro_bin), os.environ["PATH"])

    def test_maestro_path_is_added_for_later_runtime_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            maestro_bin = home / ".maestro" / "bin"
            maestro_bin.mkdir(parents=True)
            env = {"PATH": "/existing"}

            additions = bootstrap.environment.add_maestro_path(
                env=env,
                home=home,
            )

        self.assertEqual(additions, (str(maestro_bin),))
        self.assertEqual(env["PATH"], f"{maestro_bin}{os.pathsep}/existing")

    def test_maestro_executable_finds_installer_binary_without_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            executable = home / ".maestro" / "bin" / "maestro.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()

            resolved = bootstrap.environment.maestro_executable(
                env={"PATH": ""},
                home=home,
            )

        self.assertEqual(resolved, str(executable))


class InitBootstrapTests(unittest.TestCase):
    @patch("apppilot.init._offer_bootstrap", return_value=False)
    @patch("apppilot.init._is_interactive", return_value=False)
    @patch("apppilot.init._load_dotenv")
    @patch("apppilot.init.run_environment_checks")
    def test_setup_only_checks_local_prerequisites(
        self,
        run_environment_checks,
        _load_dotenv,
        _is_interactive,
        _offer_bootstrap,
    ) -> None:
        run_environment_checks.return_value = [
            init.CheckResult(
                "java",
                "Environment",
                init.Importance.REQUIRED,
                init.Status.PASS,
                "Java 17",
            )
        ]
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = init.setup_main()

        self.assertEqual(exit_code, 0)
        self.assertIn("Environment", output.getvalue())
        self.assertNotIn("Model", output.getvalue())

    @patch("apppilot.init.device.list_avds", return_value=[])
    @patch(
        "apppilot.init.environment.probe_executable",
        return_value=init.environment.ExecutableProbe(found=True),
    )
    def test_emulator_selection_reports_missing_emulator_without_creating_one(
        self,
        _probe_executable,
        _list_avds,
    ) -> None:
        output: list[str] = []

        results = init._setup_emulator(
            interactive=True,
            input_fn=lambda _prompt: "",
            output=output.append,
            runner=Mock(),
            timeout=1,
            popen=Mock(),
            boot_timeout=1,
            poll_interval=0,
            clock=Mock(),
            sleep=Mock(),
            which=lambda _name: "/mock/emulator",
        )

        self.assertEqual(
            output,
            ["✗ No Android emulator is configured. Add one and try again."],
        )
        self.assertEqual(results[0].status, init.Status.FAIL)
        self.assertIn("Add an Android emulator", results[0].remediation)

    @patch("apppilot.init.bootstrap.apply_plan")
    @patch("apppilot.init.bootstrap.create_plan")
    def test_init_applies_the_displayed_plan_only_after_consent(
        self,
        create_plan,
        apply_plan,
    ) -> None:
        action = bootstrap.BootstrapAction("maestro", "Install Maestro", ("maestro",))
        create_plan.return_value = bootstrap.BootstrapPlan((action,))
        core = [
            init.CheckResult(
                "maestro",
                "Environment",
                init.Importance.REQUIRED,
                init.Status.FAIL,
                "Maestro not found",
            )
        ]
        output: list[str] = []

        applied = init._offer_bootstrap(
            core,
            requested=True,
            interactive=True,
            input_fn=lambda _prompt: "yes",
            output=output.append,
            repository=Path("."),
        )

        self.assertTrue(applied)
        self.assertIn("AppPilot can install", output[0])
        apply_plan.assert_called_once()

    @patch("apppilot.init.bootstrap.apply_plan")
    @patch("apppilot.init.bootstrap.create_plan")
    def test_init_does_not_apply_plan_when_consent_is_declined(
        self,
        create_plan,
        apply_plan,
    ) -> None:
        action = bootstrap.BootstrapAction("maestro", "Install Maestro", ("maestro",))
        create_plan.return_value = bootstrap.BootstrapPlan((action,))
        core = [
            init.CheckResult(
                "maestro",
                "Environment",
                init.Importance.REQUIRED,
                init.Status.FAIL,
                "Maestro not found",
            )
        ]

        applied = init._offer_bootstrap(
            core,
            requested=True,
            interactive=True,
            input_fn=lambda _prompt: "no",
            output=lambda _message: None,
            repository=Path("."),
        )

        self.assertFalse(applied)
        apply_plan.assert_not_called()
