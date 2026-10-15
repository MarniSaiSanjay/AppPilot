"""Bounded, sanitized failure artifacts for deeplink test runs."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ARTIFACT_ROOT = Path(".apppilot_artifacts")
DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024
_SENSITIVE_NAME = re.compile(
    r"(authorization|api[_-]?key|password|secret|token|credential|username)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)((?:authorization|x-api-key|password|api[_-]?key|secret|token)"
    r"[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|(?:bearer\s+)?[^\"'\s,;}\]]+)"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def sensitive_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        value
        for name, value in environment.items()
        if _SENSITIVE_NAME.search(name) and value
    )


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _SENSITIVE_TEXT.sub(r"\1[REDACTED]", text)
    return _EMAIL.sub("[REDACTED EMAIL]", text)


class FailureArtifactWriter:
    def __init__(
        self,
        run_id: str,
        device: str,
        apk_identity: Mapping[str, str],
        *,
        root: Path = DEFAULT_ARTIFACT_ROOT,
        max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if max_bytes < 1024:
            raise ValueError("max_bytes must be at least 1024")
        self._run_id = run_id
        self._device = device
        self._apk_identity = dict(apk_identity)
        self._directory = Path(root) / self._safe_name(run_id)
        self._max_bytes = max_bytes
        source = os.environ if environment is None else environment
        self._secret_values = sensitive_values(source)

    @property
    def directory(self) -> Path:
        return self._directory

    def write(
        self,
        *,
        case_id: str,
        attempt: int,
        expected_destination: str,
        observed_destination: str,
        matched: bool,
        cleanup_status: str,
        cleanup_errors: list[str],
        evidence: Mapping[str, object] | None = None,
    ) -> Path:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "case_id": case_id,
            "attempt": attempt,
            "device": self._device,
            "apk_identity": self._apk_identity,
            "expected_destination": expected_destination,
            "observed_destination": observed_destination,
            "matched": matched,
            "cleanup_status": cleanup_status,
            "cleanup_errors": cleanup_errors,
            "evidence": dict(evidence or {}),
        }
        encoded = self._encode_bounded(payload)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / (
            f"{self._safe_name(case_id)}-attempt-{attempt}-"
            f"{uuid.uuid4().hex[:8]}.json"
        )
        with path.open("xb") as artifact:
            artifact.write(encoded)
        return path

    def _encode_bounded(self, payload: dict) -> bytes:
        sanitized = self._sanitize(payload)
        encoded = self._encode(sanitized)
        if len(encoded) <= self._max_bytes:
            return encoded

        evidence = sanitized.get("evidence")
        if isinstance(evidence, dict):
            if "ui_observation" in evidence:
                evidence["ui_observation"] = "[truncated to artifact size limit]"
            if "fresh_ui_observation" in evidence:
                evidence["fresh_ui_observation"] = (
                    "[truncated to artifact size limit]"
                )
        encoded = self._encode(sanitized)
        if len(encoded) <= self._max_bytes:
            return encoded

        bounded = {
            key: self._truncate(value, 256)
            for key, value in sanitized.items()
            if key != "evidence"
        }
        bounded["evidence"] = {"truncated": True}
        encoded = self._encode(bounded)
        if len(encoded) > self._max_bytes:
            bounded["cleanup_errors"] = ["[truncated]"]
            bounded["observed_destination"] = "[truncated]"
            encoded = self._encode(bounded)
        if len(encoded) <= self._max_bytes:
            return encoded
        minimal = {
            "timestamp": self._truncate(sanitized["timestamp"], 64),
            "run_id": self._truncate(sanitized["run_id"], 64),
            "case_id": self._truncate(sanitized["case_id"], 64),
            "attempt": sanitized["attempt"],
            "device": self._truncate(sanitized["device"], 64),
            "matched": sanitized["matched"],
            "cleanup_status": sanitized["cleanup_status"],
            "evidence": {"truncated": True},
        }
        return json.dumps(
            minimal,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _sanitize(self, value):
        if isinstance(value, dict):
            return {
                str(key): self._sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            return self._truncate(redact_text(value, self._secret_values), 8192)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._sanitize(str(value))

    @staticmethod
    def _truncate(value, limit: int):
        if not isinstance(value, str) or len(value) <= limit:
            return value
        return value[: limit - 14] + "...[truncated]"

    @staticmethod
    def _encode(payload: dict) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
        return safe or "unknown"
