#!/usr/bin/env python3
"""High-confidence secret scan for source-controlled text files."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[1]
_SKIPPED_SUFFIXES = {
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".webp",
    ".zip",
}
_PLACEHOLDER_MARKERS = (
    "${",
    "<",
    "artifact",
    "example",
    "fake",
    "placeholder",
    "replace",
    "sample",
    "server-key",
    "secretref:",
    "sensitive",
    "test-",
    "your-",
)
_PATTERNS = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "GitHub token",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,})\b"),
    ),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "connection-string account key",
        re.compile(r"(?i)\bAccountKey=[A-Za-z0-9+/=]{20,}"),
    ),
    (
        "embedded credential",
        re.compile(
            r"""(?ix)
            ^\s*(?:export\s+)?
            ["']?[A-Z0-9_.-]*
            (?:api[_-]?key|password|secret|token|connection[_-]?string)
            [A-Z0-9_.-]*["']?
            \s*[:=]\s*
            ["']?([A-Za-z0-9][A-Za-z0-9_./+=:@-]{15,})["']?
            """
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        REPO_ROOT / line
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def scan_text(path: Path, text: str) -> list[Finding]:
    findings = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        lowered = line.casefold()
        for kind, pattern in _PATTERNS:
            for match in pattern.finditer(line):
                candidate = match.group(1) if match.lastindex else match.group(0)
                if any(marker in candidate.casefold() for marker in _PLACEHOLDER_MARKERS):
                    continue
                if (
                    candidate == candidate.upper()
                    and "_" in candidate
                    and candidate.replace("_", "").isalnum()
                ):
                    continue
                if candidate.casefold().startswith(
                    ("self.", "cls.", "os.", "env.", "config.")
                ):
                    continue
                if any(marker in lowered for marker in ("# allow-secret", "pragma: allowlist secret")):
                    continue
                findings.append(Finding(path, line_number, kind))
    return findings


def _scan_xlsx(path: Path) -> list[Finding]:
    findings = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not (
                    name.startswith("xl/")
                    and name.casefold().endswith(".xml")
                ):
                    continue
                try:
                    text = archive.read(name).decode("utf-8")
                except (KeyError, UnicodeDecodeError):
                    continue
                try:
                    root = ElementTree.fromstring(text)
                except ElementTree.ParseError:
                    continue
                display_path = path.with_name(
                    f"{path.name}!{name.replace('/', '!')}"
                )
                logical_values = []
                for element in root.iter():
                    local_name = element.tag.rsplit("}", 1)[-1]
                    if local_name in {"c", "si"}:
                        value = "".join(element.itertext())
                        if value:
                            logical_values.append(value)
                if not logical_values:
                    logical_values = [
                        value for value in root.itertext() if value
                    ]
                findings.extend(
                    scan_text(display_path, "\n".join(logical_values))
                )
    except (OSError, zipfile.BadZipFile):
        return []
    return findings


def scan(paths: list[Path] | None = None) -> list[Finding]:
    findings = []
    for path in paths or _tracked_files():
        if not path.is_file() or path.suffix.casefold() in _SKIPPED_SUFFIXES:
            continue
        if path.suffix.casefold() == ".xlsx":
            findings.extend(_scan_xlsx(path))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(path, text))
    return findings


def main() -> int:
    findings = scan()
    if findings:
        for finding in findings:
            relative = finding.path.relative_to(REPO_ROOT)
            print(
                f"{relative}:{finding.line}: possible {finding.kind}",
                file=sys.stderr,
            )
        return 1
    print("secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
