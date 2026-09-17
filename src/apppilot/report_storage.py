"""Persist suite reports locally in a readable Markdown format."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Mapping, Protocol

from . import email_render

_REPORTS_ROOT = Path(__file__).resolve().parents[2] / ".apppilot_reports"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class EmailReportSource(Protocol):
    """A suite report that can provide the same view used for email delivery."""

    def to_email_report(self) -> email_render.EmailReport:
        """Return the report view used for email delivery."""


def _safe_name(value: str) -> str:
    normalized = _SAFE_NAME.sub("-", value.strip()).strip(".-")
    return normalized or "test-suite"


def save_markdown_report(
    report: EmailReportSource,
    *,
    test_name: str,
    env: Mapping[str, str],
    now=datetime.now,
) -> Path:
    """Save the email-equivalent report body under its test-suite directory."""
    timestamp = now().strftime("%Y-%m-%d_%H-%M-%S")
    directory = _REPORTS_ROOT / _safe_name(test_name)
    directory.mkdir(parents=True, exist_ok=True)
    contact = env.get("APPPILOT_CONTACT_EMAIL")
    content = email_render.build_markdown_body(report.to_email_report(), contact)
    suffix = 1
    while True:
        name = timestamp if suffix == 1 else f"{timestamp}-{suffix}"
        path = directory / f"{name}.md"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content + "\n")
        except FileExistsError:
            suffix += 1
            continue
        return path
