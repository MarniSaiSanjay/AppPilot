"""APK and installed-package trust validation for credential-bearing runs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


EXPECTED_PACKAGE_ENV = "APPPILOT_EXPECTED_PACKAGE_ID"
TRUSTED_CERT_ENV = "APPPILOT_TRUSTED_CERT_SHA256"

_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_AAPT_PACKAGE_RE = re.compile(r"package:\s+name='([^']+)'")
_CERT_RE = re.compile(
    r"certificate SHA-256 digest:\s*"
    r"([0-9A-Fa-f]{2}(?::?[0-9A-Fa-f]{2}){31})",
    re.IGNORECASE,
)

Runner = Callable[..., subprocess.CompletedProcess]
Which = Callable[[str], str | None]


def resolve_android_tool(
    name: str,
    *,
    which: Which = shutil.which,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    """Find an Android tool on PATH or in an installed SDK build-tools folder."""
    if path := which(name):
        return path

    source = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    sdk_roots = [
        source.get("ANDROID_SDK_ROOT"),
        source.get("ANDROID_HOME"),
        source.get("LOCALAPPDATA")
        and str(Path(source["LOCALAPPDATA"]) / "Android" / "Sdk"),
        str(home / "Library" / "Android" / "sdk"),
        str(home / "Android" / "Sdk"),
    ]
    executable_names = (name, f"{name}.bat")
    candidates: list[tuple[tuple[int, ...], str, Path]] = []
    for raw_root in dict.fromkeys(root for root in sdk_roots if root):
        build_tools = Path(raw_root).expanduser() / "build-tools"
        try:
            versions = tuple(build_tools.iterdir())
        except OSError:
            continue
        for version in versions:
            if not version.is_dir():
                continue
            version_key = tuple(int(part) for part in re.findall(r"\d+", version.name))
            for executable_name in executable_names:
                candidate = version / executable_name
                if candidate.is_file() and (
                    os.name == "nt" or os.access(candidate, os.X_OK)
                ):
                    candidates.append((version_key, version.name, candidate))
    if not candidates:
        return None
    return str(max(candidates, key=lambda item: (item[0], item[1]))[2])


class ApkTrustError(RuntimeError):
    """APK inspection or installed-package trust verification failed."""


class ApkTrustConfigurationError(ApkTrustError):
    """Required APK trust configuration is missing or invalid."""


@dataclass(frozen=True)
class ApkTrustConfig:
    package_id: str
    certificate_sha256: str

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "ApkTrustConfig":
        source = os.environ if env is None else env
        package_id = (source.get(EXPECTED_PACKAGE_ENV) or "").strip()
        raw_certificate = (source.get(TRUSTED_CERT_ENV) or "").strip()
        missing = [
            name
            for name, value in (
                (EXPECTED_PACKAGE_ENV, package_id),
                (TRUSTED_CERT_ENV, raw_certificate),
            )
            if not value
        ]
        if missing:
            raise ApkTrustConfigurationError(
                "missing required APK trust setting(s): " + ", ".join(missing)
            )
        if not _PACKAGE_RE.fullmatch(package_id):
            raise ApkTrustConfigurationError(
                f"{EXPECTED_PACKAGE_ENV} is not a valid Android package ID"
            )
        certificate = normalize_certificate_fingerprint(raw_certificate)
        if certificate is None:
            raise ApkTrustConfigurationError(
                f"{TRUSTED_CERT_ENV} must be a 64-character SHA-256 fingerprint"
            )
        return cls(package_id=package_id, certificate_sha256=certificate)


@dataclass(frozen=True)
class ApkMetadata:
    package_id: str
    certificate_sha256: tuple[str, ...]


@dataclass(frozen=True)
class ForegroundComponent:
    package_id: str
    activity: str


def normalize_certificate_fingerprint(value: str) -> str | None:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", value or "").upper()
    return normalized if len(normalized) == 64 else None


def parse_foreground_component(output: str) -> ForegroundComponent | None:
    for raw in output.splitlines():
        line = raw.strip()
        if "ResumedActivity" not in line:
            continue
        match = re.search(r"\s([A-Za-z0-9_.]+)/(\S+)", line)
        if match:
            return ForegroundComponent(
                package_id=match.group(1),
                activity=f"{match.group(1)}/{match.group(2)}",
            )
    return None


def parse_activity_component(value: str) -> ForegroundComponent | None:
    match = re.fullmatch(r"\s*([A-Za-z0-9_.]+)/(\S+)\s*", value or "")
    if not match:
        return None
    return ForegroundComponent(
        package_id=match.group(1),
        activity=f"{match.group(1)}/{match.group(2)}",
    )


class ApkTrustGuard:
    """Validate a local APK and guard installed-package credential release."""

    def __init__(
        self,
        config: ApkTrustConfig,
        *,
        runner: Runner = subprocess.run,
        which: Which = shutil.which,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._which = which
        self._env = env
        self._home = home
        self._verified_device: str | None = None
        self._verified_paths: tuple[str, ...] = ()

    def validate_apk(
        self, apk_path: str | Path, timeout: float = 60
    ) -> ApkMetadata:
        deadline = time.monotonic() + timeout
        path = Path(apk_path)
        manifest_tool = self._manifest_tool()
        apksigner = self._required_tool("apksigner")
        package_id = self._inspect_package(
            path,
            manifest_tool,
            timeout=self._remaining_timeout(deadline),
        )
        certificates = self._inspect_certificates(
            path,
            apksigner,
            timeout=self._remaining_timeout(deadline),
        )
        if package_id != self.config.package_id:
            raise ApkTrustError(
                "APK package ID mismatch: expected "
                f"{self.config.package_id}, found {package_id}"
            )
        if self.config.certificate_sha256 not in certificates:
            raise ApkTrustError("APK signing certificate is not trusted")
        return ApkMetadata(package_id, certificates)

    def validate_tools(self) -> None:
        self._manifest_tool()
        self._required_tool("apksigner")

    def before_install(
        self, apk_path: str | Path, timeout: float = 60
    ) -> None:
        self._verified_device = None
        self._verified_paths = ()
        self.validate_apk(apk_path, timeout=timeout)

    def verify_installed(self, device_id: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        paths = self._installed_paths(
            device_id, timeout=self._remaining_timeout(deadline)
        )
        apksigner = self._required_tool("apksigner")
        with tempfile.TemporaryDirectory(prefix="apppilot-installed-apk-") as temp:
            installed_apk = Path(temp) / "base.apk"
            self._run_checked(
                [
                    "adb",
                    "-s",
                    device_id,
                    "pull",
                    paths[0],
                    str(installed_apk),
                ],
                "pull installed APK for trust verification",
                timeout=self._remaining_timeout(deadline),
            )
            certificates = self._inspect_certificates(
                installed_apk,
                apksigner,
                timeout=self._remaining_timeout(deadline),
            )
        if self.config.certificate_sha256 not in certificates:
            raise ApkTrustError(
                "installed package signing certificate is not trusted"
            )
        self._verified_device = device_id
        self._verified_paths = paths

    def assert_credential_release(
        self, device_id: str, timeout: float = 60
    ) -> None:
        deadline = time.monotonic() + timeout
        if self._verified_device != device_id or not self._verified_paths:
            raise ApkTrustError(
                "installed package trust was not verified before credential entry"
            )
        if (
            self._installed_paths(
                device_id,
                timeout=self._remaining_timeout(deadline),
            )
            != self._verified_paths
        ):
            raise ApkTrustError(
                "installed package changed after trust verification"
            )
        result = self._run_checked(
            [
                "adb",
                "-s",
                device_id,
                "shell",
                "dumpsys",
                "activity",
                "activities",
            ],
            "query foreground activity before credential entry",
            timeout=self._remaining_timeout(deadline),
        )
        component = parse_foreground_component(
            f"{result.stdout or ''}\n{result.stderr or ''}"
        )
        if component is None:
            raise ApkTrustError(
                "could not determine the foreground package before credential entry"
            )
        if component.package_id != self.config.package_id:
            raise ApkTrustError(
                "credential entry blocked because the expected app is not foreground"
            )

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApkTrustError(
                "credential trust verification timeout budget exhausted"
            )
        return remaining

    def _installed_paths(
        self, device_id: str, timeout: float = 60
    ) -> tuple[str, ...]:
        result = self._run_checked(
            [
                "adb",
                "-s",
                device_id,
                "shell",
                "pm",
                "path",
                self.config.package_id,
            ],
            "query installed package",
            timeout=timeout,
        )
        paths = tuple(
            line.removeprefix("package:").strip()
            for line in (result.stdout or "").splitlines()
            if line.strip().startswith("package:")
        )
        if not paths:
            raise ApkTrustError(
                f"expected package {self.config.package_id} is not installed"
            )
        base_paths = tuple(path for path in paths if path.endswith("/base.apk"))
        return base_paths or paths

    def _manifest_tool(self) -> tuple[str, str]:
        if path := resolve_android_tool(
            "apkanalyzer", which=self._which, env=self._env, home=self._home
        ):
            return path, "apkanalyzer"
        if path := resolve_android_tool(
            "aapt", which=self._which, env=self._env, home=self._home
        ):
            return path, "aapt"
        raise ApkTrustError(
            "APK inspection requires apkanalyzer or aapt on PATH or in the "
            "Android SDK"
        )

    def _required_tool(self, name: str) -> str:
        path = resolve_android_tool(
            name, which=self._which, env=self._env, home=self._home
        )
        if not path:
            raise ApkTrustError(
                f"APK inspection requires {name} on PATH or in the Android SDK"
            )
        return path

    def _inspect_package(
        self,
        path: Path,
        tool: tuple[str, str],
        *,
        timeout: float = 60,
    ) -> str:
        executable, kind = tool
        if kind == "apkanalyzer":
            result = self._run_checked(
                [executable, "manifest", "application-id", str(path)],
                "inspect APK package ID",
                timeout=timeout,
            )
            package_id = (result.stdout or "").strip().splitlines()
            value = package_id[-1].strip() if package_id else ""
        else:
            result = self._run_checked(
                [executable, "dump", "badging", str(path)],
                "inspect APK package ID",
                timeout=timeout,
            )
            match = _AAPT_PACKAGE_RE.search(
                f"{result.stdout or ''}\n{result.stderr or ''}"
            )
            value = match.group(1) if match else ""
        if not _PACKAGE_RE.fullmatch(value):
            raise ApkTrustError(
                "APK inspection did not return a valid package ID"
            )
        return value

    def _inspect_certificates(
        self, path: Path, apksigner: str, *, timeout: float = 60
    ) -> tuple[str, ...]:
        result = self._run_checked(
            [apksigner, "verify", "--print-certs", str(path)],
            "verify APK signing certificate",
            timeout=timeout,
        )
        combined = f"{result.stdout or ''}\n{result.stderr or ''}"
        certificates = tuple(
            normalized
            for match in _CERT_RE.finditer(combined)
            if (
                normalized := normalize_certificate_fingerprint(
                    match.group(1)
                )
            )
        )
        if not certificates:
            raise ApkTrustError(
                "APK signing certificate SHA-256 fingerprint could not be determined"
            )
        return tuple(dict.fromkeys(certificates))

    def _run_checked(
        self,
        command: Sequence[str],
        operation: str,
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess:
        try:
            result = self._runner(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ApkTrustError(f"{operation} failed: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ApkTrustError(
                f"{operation} failed" + (f": {detail}" if detail else "")
            )
        return result
