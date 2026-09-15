from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.scan_secrets import scan, scan_text
from scripts.validate_workbook import validate


class SourceValidationTests(unittest.TestCase):
    def test_tracked_workbook_release_invariants(self) -> None:
        validate()

    def test_secret_scan_detects_high_confidence_credentials(self) -> None:
        path = Path("sample.py")
        findings = scan_text(
            path,
            'API_KEY = "sk-1234567890abcdefghijklmnop"\n',  # allow-secret
        )

        self.assertEqual(len(findings), 2)
        self.assertEqual({finding.kind for finding in findings}, {
            "OpenAI-style key",
            "embedded credential",
        })

    def test_secret_scan_allows_documented_placeholders(self) -> None:
        findings = scan_text(
            Path("README.md"),
            'APPPILOT_MODEL_API_KEY = "your-key-here"\n',
        )

        self.assertEqual(findings, [])

    def test_secret_scan_detects_quoted_credential_assignment(self) -> None:
        findings = scan_text(
            Path("config.py"),
            'APPPILOT_API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\n',  # allow-secret
        )

        self.assertEqual(
            [finding.kind for finding in findings],
            ["embedded credential"],
        )

    def test_secret_scan_detects_unquoted_dotenv_assignment(self) -> None:
        findings = scan_text(
            Path(".env"),
            "APPPILOT_API_KEY=abcdefghijklmnopqrstuvwxyz123456\n",  # allow-secret
        )

        self.assertEqual(len(findings), 1)

    def test_secret_scan_detects_unquoted_shell_assignment(self) -> None:
        findings = scan_text(
            Path("setup.sh"),
            "export APPPILOT_API_KEY=abcdefghijklmnopqrstuvwxyz123456\n",  # allow-secret
        )

        self.assertEqual(len(findings), 1)

    def test_secret_scan_detects_yaml_credential_assignment(self) -> None:
        findings = scan_text(
            Path("config.yml"),
            "APPPILOT_API_KEY: abcdefghijklmnopqrstuvwxyz123456\n",  # allow-secret
        )

        self.assertEqual(len(findings), 1)

    def test_secret_scan_allows_synthetic_fixture(self) -> None:
        findings = scan_text(
            Path("fixture.env"),
            "APPPILOT_API_KEY=fake-credential-for-test-fixture\n",
        )

        self.assertEqual(findings, [])

    def test_secret_inside_xlsx_textual_content_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "xl/sharedStrings.xml",
                    "<sst><si><t>APPPILOT_API_KEY="
                    "abcdefghijklmnopqrstuvwxyz123456</t></si></sst>",  # allow-secret
                )

            findings = scan([path])

        self.assertEqual(len(findings), 1)
        self.assertIn("sharedStrings.xml", str(findings[0].path))

    def test_secret_split_across_xlsx_rich_text_runs_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "xl/sharedStrings.xml",
                    "<sst><si><r><t>APPPILOT_API_KEY=</t></r>"
                    "<r><t>abcdefghijklmnopqrstuvwxyz123456</t></r>"
                    "</si></sst>",  # allow-secret
                )

            findings = scan([path])

        self.assertEqual(len(findings), 1)

    def test_ordinary_xlsx_xml_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    '<worksheet><sheetData><row r="1"><c t="inlineStr">'
                    "<is><t>Expected Result</t></is></c></row></sheetData>"
                    "</worksheet>",
                )
                archive.writestr(
                    "docProps/core.xml",
                    "<coreProperties><creator>AppPilot</creator>"
                    "</coreProperties>",
                )

            findings = scan([path])

        self.assertEqual(findings, [])

    def test_pyflakes_fails_for_a_new_unused_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unused.py"
            path.write_text("import json\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-m", "pyflakes", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("imported but unused", result.stdout)


if __name__ == "__main__":
    unittest.main()
