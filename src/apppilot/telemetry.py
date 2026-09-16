"""Minimal per-suite telemetry."""

from __future__ import annotations

import json
import os
import platform
import urllib.request
from typing import Callable, Mapping, Optional


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _default_transport(request: urllib.request.Request) -> None:
    with _OPENER.open(request, timeout=10):
        return None


def _host_os() -> Optional[str]:
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
    transport: Callable[[urllib.request.Request], None] | None = None,
) -> None:
    """POST one telemetry record."""
    try:
        env = os.environ if env is None else env
        url = (env.get("APPPILOT_TELEMETRY_URL") or "").strip()
        key = (env.get("APPPILOT_TELEMETRY_API_KEY") or "").strip()
        host_os = _host_os()
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
        (transport or _default_transport)(request)
    except (Exception, KeyboardInterrupt):
        pass
