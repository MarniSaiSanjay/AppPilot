"""Minimal per-suite telemetry (roadmap #9).

One best-effort record per real suite run - suite, test_cases, host_os
(macOS/Windows), target_platform - sent by a single HTTPS POST to the relay's
``/telemetry`` endpoint, which owns the Azure Storage credential (none lives in
this CLI). Never raises, so it can never affect the suite verdict, exit code, or
timing. Config (env/.env): APPPILOT_TELEMETRY_URL, APPPILOT_EMAIL_API_KEY.
"""

from __future__ import annotations

import json
import os
import platform
import urllib.request
from typing import Mapping, Optional


def _host_os() -> Optional[str]:
    # macOS and native Windows map directly; WSL (a Linux kernel with a Microsoft
    # marker) counts as Windows. Any other host is unsupported -> no record.
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        return "Windows"
    if system == "Linux" and (
        "microsoft" in platform.release().lower()
        or os.environ.get("WSL_DISTRO_NAME")
        or os.environ.get("WSL_INTEROP")
    ):
        return "Windows"
    return None


def record_suite_run(
    suite: str,
    test_cases: int,
    target_platform: str,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Best-effort: POST one telemetry record. Never raises."""
    try:
        env = os.environ if env is None else env
        url = (env.get("APPPILOT_TELEMETRY_URL") or "").strip()
        key = (env.get("APPPILOT_EMAIL_API_KEY") or "").strip()
        host_os = _host_os()
        # Skip silently if unconfigured, non-TLS (never leak the key), or an
        # unsupported host.
        if not url.startswith("https://") or not key or host_os is None:
            return
        body = json.dumps(
            {
                "suite": suite,
                "test_cases": test_cases,
                "host_os": host_os,
                "target_platform": target_platform,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "X-API-Key": key},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=10).close()
    except Exception:
        pass  # telemetry is best-effort; the suite result is independent of it
