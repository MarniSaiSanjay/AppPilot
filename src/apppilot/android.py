"""Android/Maestro infrastructure: UI observation and action execution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from xml.etree import ElementTree
from pathlib import Path
from typing import Callable, Sequence

from . import logtags
from .apk_trust import (
    ForegroundComponent,
    parse_activity_component,
    parse_foreground_component,
)
from .models import (
    Action,
    ActionKind,
    CredentialInputMethod,
    CredentialKind,
    CredentialVerification,
    CredentialVerificationStatus,
    UIElement,
    UIObservation,
)
from .safety import infer_credential_kind

APP_ID = "com.microsoft.office.officehubrow"


MAESTRO_SECRET_ENV = "MAESTRO_APPPILOT_INPUT_SECRET"

_TRANSIENT_DRIVER_FAILURE_MARKERS = (
    "driver did not start up in time",
    "device server died during 'deviceinfo'",
)
_DRIVER_STARTUP_MAX_ATTEMPTS = 3
_DRIVER_STARTUP_RETRY_DELAY = 3.0
_DRIVER_STARTUP_TIMEOUT_ENV = "MAESTRO_DRIVER_STARTUP_TIMEOUT"
_DRIVER_STARTUP_TIMEOUT_MS = "120000"
_MAESTRO_RUNTIME_ENV_KEYS = (
    "PATH",
    "HOME",
    "JAVA_HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "USER",
    "USERNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)
_ADB_SAFE_USERNAME = re.compile(r"[\x21-\x7e]+\Z")
_ADB_USERNAME_CHARACTER_DELAY_SECONDS = 0.10
_PASSWORD_MASK_CHARACTERS = frozenset("*•●·∙▪◦")
_APP_LINK_DOMAIN = re.compile(
    r"(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_APP_LINK_APPROVED_STATES = {
    "approved",
    "migrated",
    "pre_verified",
    "restored",
    "system_configured",
    "verified",
    "1",
    "2",
    "4",
    "5",
    "7",
    "8",
}


def _nonnegative_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a non-negative integer") from error
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nonnegative_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise ValueError(
            f"{name} must be a finite non-negative number"
        ) from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return value


def _maestro_environment(
    secret: str | None = None,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if source is None else source
    environment = {
        name: source[name]
        for name in _MAESTRO_RUNTIME_ENV_KEYS
        if source.get(name)
    }
    environment[_DRIVER_STARTUP_TIMEOUT_ENV] = _DRIVER_STARTUP_TIMEOUT_MS
    if secret is not None:
        environment[MAESTRO_SECRET_ENV] = secret
    return environment


class AndroidOperationalError(RuntimeError):
    """Expected device/Maestro infrastructure failure."""


@dataclass(frozen=True)
class SupportedLinkState:
    user_id: str
    domains: tuple[str, ...]
    handling_allowed: bool
    enabled_domains: tuple[str, ...]
    verification_states: tuple[tuple[str, str], ...]


class MaestroHierarchyObserver:
    _BASE_EXCLUDED_PREFIXES = (
        "com.android.systemui:",
        "android:id/input_method_",
    )

    def __init__(
        self,
        device_id: str,
        max_elements: int = 100,
        *,
        ime_package_provider=None,
    ) -> None:
        self._device_id = device_id
        self._max_elements = max_elements
        self._ime_provider = ime_package_provider
        self._excluded_prefixes = self._BASE_EXCLUDED_PREFIXES
        self._ime_resolved = False
        self._popup_unblock_budget_limit = _nonnegative_env_int(
            "APPPILOT_POPUP_UNBLOCK_BUDGET",
            3,
        )
        self._popup_unblock_budget = self._popup_unblock_budget_limit
        self._popup_unblock_settle_seconds = _nonnegative_env_float(
            "APPPILOT_POPUP_UNBLOCK_SETTLE",
            1.0,
        )
        self._pending_credential_check: (
            tuple[CredentialKind, str, bytes, int] | None
        ) = None
        self._capture_credential_verification: CredentialVerification | None = None

    def reset_recovery_budget(self) -> None:
        """Reset bounded blank-popup recovery for a new lifecycle attempt."""
        self._popup_unblock_budget = self._popup_unblock_budget_limit

    def expect_credential(
        self,
        kind: CredentialKind,
        field_id: str,
        expected: str,
    ) -> None:
        """Verify one credential locally during the next normal observation."""
        self._pending_credential_check = (
            kind,
            field_id,
            hashlib.sha256(expected.encode("utf-8")).digest(),
            len(expected),
        )

    def observe(self) -> UIObservation:
        self._ensure_excluded_prefixes()
        try:
            observation = self._capture()
            if self._is_autofill_overlay(observation):
                recovered = self._dismiss_autofill_overlay()
                if recovered is not None:
                    observation = recovered
            elif self._is_blank(observation):
                recovered = self._unblock_blank_screen()
                if recovered is not None:
                    observation = recovered
            return observation
        finally:
            self._pending_credential_check = None

    def reobserve(self) -> UIObservation:
        """Capture a fresh observation before executing a model decision."""
        return self.observe()

    def _capture(self) -> UIObservation:
        error: AndroidOperationalError | None = None
        for attempt in range(2):
            try:
                return self._capture_uiautomator()
            except AndroidOperationalError as current_error:
                error = current_error
                if (
                    attempt == 0
                    and "invalid UI hierarchy" in str(current_error)
                ):
                    time.sleep(0.25)
                    continue
                break
        print(
            logtags.prefix(
                logtags.MAESTRO,
                f"direct hierarchy unavailable; using Maestro fallback: {error}",
            )
        )
        try:
            return self._capture_maestro()
        except AndroidOperationalError as maestro_error:
            time.sleep(0.5)
            try:
                return self._capture_uiautomator()
            except AndroidOperationalError as recovery_error:
                raise maestro_error from recovery_error

    def _capture_uiautomator(self, timeout: float = 15) -> UIObservation:
        command = [
            "adb",
            "-s",
            self._device_id,
            "exec-out",
            "uiautomator",
            "dump",
            "/dev/tty",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise AndroidOperationalError(
                f"Android hierarchy observation failed: {error}"
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise AndroidOperationalError(
                "Android hierarchy observation failed"
                + (f": {detail}" if detail else "")
            )

        end = result.stdout.rfind("</hierarchy>")
        if end < 0:
            raise AndroidOperationalError(
                "Android returned an invalid UI hierarchy"
            )
        xml = result.stdout[: end + len("</hierarchy>")]
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as error:
            raise AndroidOperationalError(
                "Android returned an invalid UI hierarchy"
            ) from error

        elements: list[UIElement] = []
        self._capture_credential_verification = None
        self._collect(
            self._xml_as_maestro_node(root),
            (),
            None,
            False,
            elements,
        )
        return self._observation(elements)

    @classmethod
    def _xml_as_maestro_node(cls, element: ElementTree.Element) -> dict:
        attributes = dict(element.attrib)
        attributes["accessibilityText"] = attributes.pop("content-desc", "")
        attributes["hintText"] = attributes.pop("hint", "")
        return {
            "attributes": attributes,
            "children": [
                cls._xml_as_maestro_node(child)
                for child in element
            ],
        }

    def _capture_maestro(self, timeout: float = 30) -> UIObservation:
        command = [
            "maestro",
            "--no-ansi",
            "--udid",
            self._device_id,
            "hierarchy",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_maestro_environment(),
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise AndroidOperationalError(
                f"Maestro hierarchy observation failed: {error}"
            ) from error
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            raise AndroidOperationalError(
                f"Maestro hierarchy observation failed: {error}"
            )

        try:
            hierarchy = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AndroidOperationalError(
                "Maestro returned an invalid UI hierarchy"
            ) from error

        elements: list[UIElement] = []
        self._capture_credential_verification = None
        self._collect(hierarchy, (), None, False, elements)
        return self._observation(elements)

    def _observation(self, elements: list[UIElement]) -> UIObservation:
        verification = self._capture_credential_verification
        if self._pending_credential_check is not None and verification is None:
            kind, field_id, _, _ = self._pending_credential_check
            verification = CredentialVerification(
                kind=kind,
                field_id=field_id,
                element_id=None,
                status=CredentialVerificationStatus.UNAVAILABLE,
            )
        return UIObservation(
            tuple(elements[: self._max_elements]),
            credential_verification=verification,
        )

    @staticmethod
    def _is_blank(observation: UIObservation) -> bool:
        """True when no observed element carries usable semantics."""
        return not any(
            e.label or e.resource_id or e.clickable or e.is_input
            for e in observation.elements
        )

    @staticmethod
    def _is_autofill_overlay(observation: UIObservation) -> bool:
        """True for Android's focused autofill-settings overlay."""
        labels = {
            element.label.casefold()
            for element in observation.elements
            if element.label
        }
        return bool(labels) and labels <= {"autofill settings"}

    def _dismiss_autofill_overlay(self) -> "UIObservation | None":
        if self._popup_unblock_budget <= 0:
            return None
        self._popup_unblock_budget -= 1
        if not self._press_back():
            return None
        time.sleep(0.2)
        return self._capture()

    def _unblock_blank_screen(self) -> "UIObservation | None":
        """Recover a blank observation caused by a separate focused window."""
        if self._popup_unblock_budget <= 0:
            return None
        if not self._focused_window_is_popup():
            return None
        self._popup_unblock_budget -= 1
        if not self._press_back():
            return None
        time.sleep(self._popup_unblock_settle_seconds)
        return self._capture()

    def _focused_window_is_popup(self, timeout: float = 15) -> bool:
        """True when a separate pop-up window owns focus."""
        try:
            result = subprocess.run(
                ["adb", "-s", self._device_id, "shell", "dumpsys", "window"],
                check=False, capture_output=True, text=True, timeout=timeout,
            )
        except Exception:
            return False
        for line in (result.stdout or "").splitlines():
            if "mCurrentFocus" in line and "pop-up window" in line.lower():
                return True
        return False

    def _press_back(self, timeout: float = 15) -> bool:
        try:
            result = subprocess.run(
                [
                    "adb",
                    "-s",
                    self._device_id,
                    "shell",
                    "input",
                    "keyevent",
                    "KEYCODE_BACK",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _ensure_excluded_prefixes(self) -> None:
        """Resolve the active IME package once and add it to the excluded set."""
        if self._ime_resolved:
            return
        self._ime_resolved = True
        try:
            package = (self._ime_provider or self._query_ime_package)()
        except Exception:
            package = None
        if package:
            self._excluded_prefixes = self._excluded_prefixes + (f"{package}:",)

    def _query_ime_package(self, timeout: float = 10) -> "str | None":
        result = subprocess.run(
            [
                "adb",
                "-s",
                self._device_id,
                "shell",
                "settings",
                "get",
                "secure",
                "default_input_method",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        value = (result.stdout or "").strip()
        if not value or value == "null":
            return None

        return value.split("/", 1)[0]

    def _collect(
        self,
        node: dict,
        path: tuple[int, ...],
        parent_id: str | None,
        in_system_ui: bool,
        elements: list[UIElement],
    ) -> list[str]:
        attributes = node.get("attributes", {})
        element_id = "e:" + (".".join(map(str, path)) if path else "root")
        resource_id = self._clean(attributes.get("resource-id"), limit=200)
        system_ui = in_system_ui or resource_id.startswith(self._excluded_prefixes)
        text = self._clean(attributes.get("text"))
        accessibility_text = self._clean(attributes.get("accessibilityText"))
        hint_text = self._clean(attributes.get("hintText"))
        class_name = self._clean(attributes.get("class"), limit=120)
        package_name = self._clean(attributes.get("package"), limit=200)
        clickable = self._as_bool(attributes.get("clickable", node.get("clickable")))
        enabled = self._as_bool(attributes.get("enabled", node.get("enabled")), True)
        is_input = class_name.endswith("EditText") or "TextInput" in class_name

        field_credential_kind = None
        if is_input:
            field_credential_kind = (
                CredentialKind.PASSWORD
                if self._as_bool(
                    attributes.get("password", node.get("password"))
                )
                else infer_credential_kind(
                    resource_id,
                    hint_text,
                    class_name,
                    accessibility_text,
                )
            )
        if (
            self._pending_credential_check is not None
            and field_credential_kind is not None
        ):
            (
                expected_kind,
                expected_field_id,
                expected_digest,
                expected_length,
            ) = (
                self._pending_credential_check
            )
            if (
                field_credential_kind == expected_kind
                and expected_field_id in (resource_id, element_id)
            ):
                self._capture_credential_verification = CredentialVerification(
                    kind=expected_kind,
                    field_id=expected_field_id,
                    element_id=element_id,
                    status=(
                        CredentialVerificationStatus.MATCHED
                        if self._credential_value_matches(
                            expected_kind,
                            text,
                            expected_digest,
                            expected_length,
                        )
                        else CredentialVerificationStatus.MISMATCHED
                    ),
                )
        if field_credential_kind is not None:
            text = ""
            accessibility_text = field_credential_kind.value

        own_labels = [value for value in (text, accessibility_text, hint_text) if value]
        potentially_useful = bool(own_labels or clickable or is_input)
        child_parent_id = element_id if potentially_useful else parent_id
        child_labels: list[str] = []
        for index, child in enumerate(node.get("children", [])):
            child_labels.extend(
                self._collect(
                    child,
                    path + (index,),
                    child_parent_id,
                    system_ui,
                    elements,
                )
            )

        descendant_labels = self._unique(child_labels)[:3]
        label_parts = own_labels or (descendant_labels if clickable or is_input else [])
        label = " | ".join(self._unique(label_parts))

        useful = bool(label or clickable or is_input)
        if useful and not system_ui and len(elements) < self._max_elements:
            elements.append(
                UIElement(
                    element_id=element_id,
                    parent_id=parent_id,
                    text=text,
                    accessibility_text=accessibility_text,
                    hint_text=hint_text,
                    resource_id=resource_id,
                    class_name=class_name,
                    clickable=clickable,
                    enabled=enabled,
                    is_input=is_input,
                    label=label,
                    bounds=self._parse_bounds(attributes.get("bounds")),
                    checked=self._optional_bool(attributes.get("checked")),
                    package_name=package_name,
                )
            )

        return [] if system_ui else self._unique(own_labels + child_labels)[:8]

    @staticmethod
    def _credential_value_matches(
        kind: CredentialKind,
        observed: str,
        expected_digest: bytes,
        expected_length: int,
    ) -> bool:
        if hashlib.sha256(observed.encode("utf-8")).digest() == expected_digest:
            return True
        return (
            kind == CredentialKind.PASSWORD
            and len(observed) == expected_length
            and bool(observed)
            and set(observed) <= _PASSWORD_MASK_CHARACTERS
        )

    @staticmethod
    def _parse_bounds(value: object) -> "tuple[int, int, int, int] | None":
        """Parse Maestro/UIAutomator bounds."""
        if not isinstance(value, str):
            return None
        numbers = re.findall(r"-?\d+", value)
        if len(numbers) != 4:
            return None
        left, top, right, bottom = (int(n) for n in numbers)
        if right < left or bottom < top:
            return None
        return (left, top, right, bottom)

    @staticmethod
    def _clean(value: object, limit: int = 240) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:limit]

    @staticmethod
    def _as_bool(value: object, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == "true"
        return default

    @staticmethod
    def _optional_bool(value: object) -> "bool | None":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        return None

    @staticmethod
    def _unique(values: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))



class MaestroExecutor:
    def __init__(
        self,
        app_id: str,
        device_id: str,
        *,
        credential_release_check: Callable[[float], None] | None = None,
        credential_observer: MaestroHierarchyObserver | None = None,
    ) -> None:
        self._app_id = app_id
        self._device_id = device_id
        self._credential_release_check = credential_release_check
        self._credential_observer = credential_observer
        self._last_deeplink_component: ForegroundComponent | None = None

    def bind_credential_observer(
        self, observer: MaestroHierarchyObserver
    ) -> None:
        self._credential_observer = observer

    def execute(
        self,
        action: Action,
        observation: UIObservation,
        secret: str | None = None,
    ) -> CredentialInputMethod | None:
        if action.kind == ActionKind.PRESS_BACK:
            self._run_adb_checked(
                ["shell", "input", "keyevent", "KEYCODE_BACK"],
                operation="press back",
            )
            return

        target = observation.find(action.target_id)
        if target is None:
            raise ValueError("Cannot execute an action without an observed target")

        if action.kind == ActionKind.TAP:
            self._tap(target, observation)
            return
        if action.kind == ActionKind.INPUT_TEXT:
            return self._input_text(action, target, secret)
        raise ValueError(f"Unsupported action kind: {action.kind}")

    def execute_fast(
        self,
        action: Action,
        observation: UIObservation,
        secret: str | None = None,
    ) -> CredentialInputMethod | None:
        """Execute an action selected from the current observation."""
        if action.kind == ActionKind.PRESS_BACK:
            self._run_adb_checked(
                ["shell", "input", "keyevent", "4"],
                operation="press back",
            )
            return
        target = observation.find(action.target_id)
        if (
            action.kind == ActionKind.TAP
            and target is not None
            and target.center is not None
        ):
            try:
                point = self._safe_tap_point(target, observation)
            except AndroidOperationalError:
                kind, _ = self._tap_command(target)
                if kind != "flow":
                    raise
                self._tap(target, observation)
                return
            self._tap_point(*point)
            return
        return self.execute(action, observation, secret=secret)

    def execute_credential_fallback(
        self,
        action: Action,
        observation: UIObservation,
        secret: str,
    ) -> CredentialInputMethod:
        """Replace one credential through the exact local clipboard path."""
        if action.kind != ActionKind.INPUT_TEXT or action.credential_kind is None:
            raise ValueError("Credential fallback requires a credential input action")
        target = observation.find(action.target_id)
        if target is None:
            raise ValueError("Cannot replace a credential without an observed target")
        target = self._revalidate_credential_target(
            action.credential_kind, target
        )
        self._input_credential_via_clipboard(
            target,
            secret,
            action.credential_kind,
        )
        return CredentialInputMethod.CLIPBOARD

    def open_link(self, deep_link: str, timeout: float = 180) -> None:
        """Launch an exact deep link through Android's VIEW intent."""
        result = self._run_adb_checked(
            [
                "shell",
                "am",
                "start",
                "-W",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                shlex.quote(deep_link),
            ],
            timeout=timeout,
            operation="open deeplink",
        )
        combined = f"{result.stdout or ''}{result.stderr or ''}"
        if "Error:" in combined or "unable to resolve Intent" in combined:
            raise AndroidOperationalError(
                f"adb open deeplink failed: {combined.strip()}"
            )
        status = next(
            (
                line.split(":", 1)[1].strip().casefold()
                for line in combined.splitlines()
                if line.strip().casefold().startswith("status:")
            ),
            "",
        )
        if status and status != "ok":
            raise AndroidOperationalError(
                f"adb open deeplink returned status {status}"
            )
        self._last_deeplink_component = next(
            (
                component
                for line in combined.splitlines()
                if line.strip().casefold().startswith("activity:")
                and (
                    component := parse_activity_component(
                        line.split(":", 1)[1].strip()
                    )
                )
                is not None
            ),
            None,
        )

    @property
    def last_deeplink_component(self) -> ForegroundComponent | None:
        return self._last_deeplink_component

    @property
    def app_id(self) -> str:
        return self._app_id

    def launch_app(self, timeout: float = 30) -> None:
        """Launch the app (used e.g. by first-install warm-up)."""
        self.launch_app_via_adb(timeout=timeout)

    def launch_app_via_open_btn_click(self, timeout: float = 60) -> None:
        """Launch the app by tapping the store's "Open" button."""
        self._run_flow('- tapOn:\n    text: "Open"\n', timeout=timeout)

    def launch_app_via_adb(self, timeout: float = 30) -> None:
        """Launch the app via its default launcher activity."""
        result = self._run_adb_checked(
            [
                "shell",
                "monkey",
                "-p",
                self._app_id,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout=timeout,
            operation="launch app",
        )
        combined = f"{result.stdout or ''}{result.stderr or ''}"
        if result.returncode != 0 or "No activities found" in combined:
            raise RuntimeError(
                f"adb launch failed for {self._app_id}: {combined.strip()}"
            )

    def stop_app(self, timeout: float = 180) -> None:
        """Force-stop (kill) the app."""
        self._run_adb_checked(
            ["shell", "am", "force-stop", self._app_id],
            timeout=timeout,
            operation="stop app",
        )

    def is_installed(self, timeout: float = 60) -> bool:
        """Return whether the app package is currently installed (via adb)."""
        result = self._run_adb_checked(
            ["shell", "pm", "list", "packages", self._app_id],
            timeout=timeout,
            operation="query installed packages",
        )
        expected = f"package:{self._app_id}"
        return expected in {
            line.strip() for line in (result.stdout or "").splitlines()
        }

    def is_foreground(self, timeout: float = 15) -> bool:
        """Return whether the target app package is foreground."""
        component = self.foreground_component(timeout=timeout)
        return component is not None and component.package_id == self._app_id

    def foreground_component(
        self, timeout: float = 15
    ) -> ForegroundComponent | None:
        """Return the currently resumed Android package/activity, if observable."""
        result = self._run_adb_checked(
            ["shell", "dumpsys", "activity", "activities"],
            timeout=timeout,
            operation="query foreground activity",
        )
        return parse_foreground_component(
            f"{result.stdout or ''}\n{result.stderr or ''}"
        )

    def uninstall_app(self, timeout: float = 180) -> None:
        """Uninstall the app package and fail if adb does not confirm success."""
        result = self._run_adb_checked(
            ["uninstall", self._app_id],
            timeout=timeout,
            operation="uninstall app",
        )
        combined = f"{result.stdout or ''}{result.stderr or ''}"
        if "Success" not in combined:
            raise AndroidOperationalError(
                f"adb uninstall did not confirm success: {combined.strip()}"
            )

    def ensure_uninstalled(self, timeout: float = 180) -> bool:
        """Ensure the app package is uninstalled."""
        deadline = time.monotonic() + timeout
        if self.is_installed(
            timeout=self._remaining_deadline(
                deadline, "query installed package"
            )
        ):
            self.uninstall_app(
                timeout=self._remaining_deadline(deadline, "uninstall app")
            )
            if self.is_installed(
                timeout=self._remaining_deadline(
                    deadline, "confirm package uninstall"
                )
            ):
                raise AndroidOperationalError(
                    f"package {self._app_id} is still installed after uninstall"
                )
            return True
        return False

    def install_apk(self, apk_path: str, timeout: float = 300) -> None:
        """Install a local APK via adb."""
        result = self._run_adb_checked(
            ["install", "-r", apk_path],
            timeout=timeout,
            operation="install APK",
        )
        combined = f"{result.stdout or ''}{result.stderr or ''}"
        if result.returncode != 0 or "Success" not in combined:
            raise AndroidOperationalError(
                f"adb install failed for {apk_path}: {combined.strip()}"
            )

    def enable_supported_links(
        self, domains: Sequence[str], timeout: float = 180
    ) -> None:
        """Approve declared App Link domains for the current Android user."""
        normalized = tuple(
            dict.fromkeys(domain.strip().casefold() for domain in domains)
        )
        if not normalized:
            raise ValueError("At least one supported-link domain is required")
        if invalid := tuple(
            domain
            for domain in normalized
            if not _APP_LINK_DOMAIN.fullmatch(domain)
        ):
            raise ValueError(
                "Invalid supported-link domain(s): " + ", ".join(invalid)
            )

        deadline = time.monotonic() + timeout
        user_id = self._current_user_id(
            timeout=self._remaining_deadline(
                deadline, "query current Android user"
            )
        )

        def run_pm(operation: str, *args: str) -> subprocess.CompletedProcess:
            return self._run_adb_checked(
                ["shell", "pm", *args],
                timeout=self._remaining_deadline(deadline, operation),
                operation=operation,
            )

        run_pm(
            "enable supported-link handling",
            "set-app-links-allowed",
            "--user",
            user_id,
            "--package",
            self._app_id,
            "true",
        )
        run_pm(
            "approve supported-link domains",
            "set-app-links",
            "--package",
            self._app_id,
            "2",
            *normalized,
        )
        run_pm(
            "select supported-link domains",
            "set-app-links-user-selection",
            "--user",
            user_id,
            "--package",
            self._app_id,
            "true",
            *normalized,
        )
        status = run_pm(
            "verify supported-link configuration",
            "get-app-links",
            "--user",
            user_id,
            self._app_id,
        )
        self._verify_supported_links(status.stdout or "", normalized)

    def capture_supported_link_state(
        self, domains: Sequence[str], timeout: float = 180
    ) -> SupportedLinkState:
        normalized = tuple(
            dict.fromkeys(domain.strip().casefold() for domain in domains)
        )
        deadline = time.monotonic() + timeout
        user_id = self._current_user_id(
            timeout=self._remaining_deadline(
                deadline, "query current Android user"
            )
        )
        status = self._run_adb_checked(
            [
                "shell",
                "pm",
                "get-app-links",
                "--user",
                user_id,
                self._app_id,
            ],
            timeout=self._remaining_deadline(
                deadline, "capture supported-link configuration"
            ),
            operation="capture supported-link configuration",
        )
        handling_allowed, enabled = self._supported_link_selection(
            status.stdout or ""
        )
        verification_states = self._supported_link_verification_states(
            status.stdout or ""
        )
        verification_states = {
            domain: verification_states.get(domain, "none")
            for domain in normalized
        }
        return SupportedLinkState(
            user_id=user_id,
            domains=normalized,
            handling_allowed=handling_allowed,
            enabled_domains=tuple(
                domain for domain in normalized if domain in enabled
            ),
            verification_states=tuple(sorted(verification_states.items())),
        )

    def restore_supported_link_state(
        self, state: SupportedLinkState, timeout: float = 180
    ) -> None:
        deadline = time.monotonic() + timeout

        def run_pm(operation: str, *args: str) -> subprocess.CompletedProcess:
            return self._run_adb_checked(
                ["shell", "pm", *args],
                timeout=self._remaining_deadline(deadline, operation),
                operation=operation,
            )

        run_pm(
            "restore supported-link handling",
            "set-app-links-allowed",
            "--user",
            state.user_id,
            "--package",
            self._app_id,
            str(state.handling_allowed).lower(),
        )
        if state.domains:
            run_pm(
                "clear temporary supported-link selection",
                "set-app-links-user-selection",
                "--user",
                state.user_id,
                "--package",
                self._app_id,
                "false",
                *state.domains,
            )
        if state.enabled_domains:
            run_pm(
                "restore supported-link selection",
                "set-app-links-user-selection",
                "--user",
                state.user_id,
                "--package",
                self._app_id,
                "true",
                *state.enabled_domains,
            )
        status = run_pm(
            "query restored supported-link state",
            "get-app-links",
            "--user",
            state.user_id,
            self._app_id,
        )
        restored_states = self._supported_link_verification_states(
            status.stdout or ""
        )
        expected_states = {
            domain: verification_state
            for domain, verification_state in dict(state.verification_states).items()
            if verification_state
            in {"none", "0", "approved", "2", "denied", "3"}
        }
        verification_domains = tuple(expected_states)
        reset_domains = [
            domain
            for domain in verification_domains
            if expected_states.get(domain, "none") in {"none", "0"}
            and restored_states.get(domain, "none") not in {"none", "0"}
        ]
        approved_domains = [
            domain
            for domain in verification_domains
            if expected_states.get(domain) in {"approved", "2"}
            and restored_states.get(domain) not in {"approved", "2"}
        ]
        denied_domains = [
            domain
            for domain in verification_domains
            if expected_states.get(domain) in {"denied", "3"}
            and restored_states.get(domain) not in {"denied", "3"}
        ]
        for code, domains in (
            ("0", reset_domains),
            ("2", approved_domains),
            ("3", denied_domains),
        ):
            if domains:
                run_pm(
                    "restore supported-link verification state",
                    "set-app-links",
                    "--package",
                    self._app_id,
                    code,
                    *domains,
                )
        expected_enabled = set(state.enabled_domains)
        status = run_pm(
            "verify restored supported-link state",
            "get-app-links",
            "--user",
            state.user_id,
            self._app_id,
        )
        restored_states = self._supported_link_verification_states(
            status.stdout or ""
        )
        handling_allowed, enabled = self._supported_link_selection(
            status.stdout or ""
        )
        changed = [
            domain
            for domain in verification_domains
            if restored_states.get(domain, "none")
            != expected_states.get(domain, "none")
        ]
        selection_changed = {
            domain for domain in state.domains if domain in enabled
        } != expected_enabled
        handling_changed = handling_allowed != state.handling_allowed
        if changed or selection_changed or handling_changed:
            problems = changed
            if selection_changed:
                problems.append("user selection")
            if handling_changed:
                problems.append("link handling")
            raise AndroidOperationalError(
                "supported-link state changed unexpectedly: "
                + ", ".join(problems)
            )

    def _current_user_id(self, timeout: float = 180) -> str:
        user_result = self._run_adb_checked(
            ["shell", "am", "get-current-user"],
            timeout=timeout,
            operation="query current Android user",
        )
        user_id = (user_result.stdout or "").strip()
        if not user_id.isdigit():
            raise AndroidOperationalError(
                "adb query current Android user returned an invalid user id"
            )
        return user_id

    @staticmethod
    def _supported_link_selection(status: str) -> tuple[bool, set[str]]:
        enabled: set[str] = set()
        section = ""
        handling_allowed = False
        for raw_line in status.splitlines():
            line = raw_line.strip().casefold()
            if line == "selection state:":
                section = "selection"
            elif line == "enabled:" and section == "selection":
                section = "enabled"
            elif line == "disabled:":
                section = "disabled"
            elif line == "verification link handling allowed: true":
                handling_allowed = True
            elif section == "enabled" and line:
                enabled.add(line)
        return handling_allowed, enabled

    @staticmethod
    def _supported_link_verification_states(status: str) -> dict[str, str]:
        states: dict[str, str] = {}
        section = ""
        for raw_line in status.splitlines():
            line = raw_line.strip().casefold()
            if line == "domain verification state:":
                section = "verification"
            elif line == "selection state:":
                section = "selection"
            elif line.startswith("verification link handling allowed:"):
                section = ""
            elif section == "verification" and ":" in line:
                domain, state = (part.strip() for part in line.rsplit(":", 1))
                states[domain] = state
        return states

    @classmethod
    def _verify_supported_links(
        cls, status: str, domains: Sequence[str]
    ) -> None:
        approved: set[str] = set()
        enabled: set[str] = set()
        section = ""
        handling_allowed = False
        for raw_line in status.splitlines():
            line = raw_line.strip().casefold()
            if line == "domain verification state:":
                section = "verification"
            elif line == "selection state:":
                section = "selection"
            elif line == "enabled:" and section == "selection":
                section = "enabled"
            elif line == "disabled:":
                section = "disabled"
            elif line == "verification link handling allowed: true":
                handling_allowed = True
            elif section == "verification" and ":" in line:
                domain, state = (part.strip() for part in line.rsplit(":", 1))
                if state in _APP_LINK_APPROVED_STATES:
                    approved.add(domain)
            elif section == "enabled" and line:
                enabled.add(line)

        missing_approval = set(domains) - approved
        missing_selection = set(domains) - enabled
        if handling_allowed and not missing_approval and not missing_selection:
            return
        problems = []
        if not handling_allowed:
            problems.append("link handling is not allowed")
        if missing_approval:
            problems.append(
                "not approved: " + ", ".join(sorted(missing_approval))
            )
        if missing_selection:
            problems.append(
                "not enabled: " + ", ".join(sorted(missing_selection))
            )
        raise AndroidOperationalError(
            "supported-link verification failed (" + "; ".join(problems) + ")"
        )

    def _run_adb(
        self, args: Sequence[str], timeout: float = 180
    ) -> subprocess.CompletedProcess:
        command = ["adb", "-s", self._device_id, *args]
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _run_adb_checked(
        self,
        args: Sequence[str],
        *,
        timeout: float = 180,
        operation: str,
    ) -> subprocess.CompletedProcess:
        try:
            result = self._run_adb(args, timeout=timeout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise AndroidOperationalError(
                f"adb {operation} failed: {error}"
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise AndroidOperationalError(
                f"adb {operation} failed"
                + (f": {detail}" if detail else "")
            )
        return result

    @staticmethod
    def _remaining_deadline(deadline: float, operation: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AndroidOperationalError(f"{operation} timeout budget exhausted")
        return remaining

    def _reset_adb_connection(self, deadline: float) -> None:
        """Restart the adb server and wait for the device to reattach."""
        for args in (["kill-server"], ["start-server"]):
            try:
                subprocess.run(
                    ["adb", *args], check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._remaining_deadline(
                        deadline, "ADB connection reset"
                    ),
                )
            except (subprocess.SubprocessError, OSError, AndroidOperationalError):
                pass
        try:
            self._run_adb(
                ["wait-for-device"],
                timeout=self._remaining_deadline(
                    deadline, "ADB device reconnect"
                ),
            )
        except (subprocess.SubprocessError, OSError, AndroidOperationalError):
            pass

    def _run_flow(
        self,
        commands: str,
        secret: str | None = None,
        timeout: float = 60,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise AndroidOperationalError(
                "Maestro action execution timeout budget exhausted"
            )
        deadline = time.monotonic() + timeout
        flow = f"appId: {self._app_id}\n---\n{commands}"
        run_env = _maestro_environment(secret)
        flow_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".yaml",
                prefix="apppilot-action-",
                delete=False,
            ) as flow_file:
                flow_file.write(flow)
                flow_path = Path(flow_file.name)

            for attempt in range(1, _DRIVER_STARTUP_MAX_ATTEMPTS + 1):
                try:
                    result = subprocess.run(
                        [
                            "maestro",
                            "--no-ansi",
                            "--udid",
                            self._device_id,
                            "test",
                            str(flow_path),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=self._remaining_deadline(
                            deadline, "Maestro action execution"
                        ),
                        env=run_env,
                    )
                except (OSError, subprocess.SubprocessError, ValueError) as error:
                    raise AndroidOperationalError(
                        f"Maestro action execution failed: {error}"
                    ) from error
                if result.returncode != 0:
                    error = result.stderr.strip() or result.stdout.strip()
                    if secret:
                        error = error.replace(secret, "***")
                    if (
                        any(
                            marker in error.lower()
                            for marker in _TRANSIENT_DRIVER_FAILURE_MARKERS
                        )
                        and attempt < _DRIVER_STARTUP_MAX_ATTEMPTS
                    ):
                        print(
                            logtags.prefix(
                                logtags.MAESTRO,
                                f"driver transport failure "
                                f"(attempt {attempt}/{_DRIVER_STARTUP_MAX_ATTEMPTS}); "
                                "resetting adb and retrying",
                            )
                        )
                        self._reset_adb_connection(deadline)
                        remaining = self._remaining_deadline(
                            deadline, "Maestro retry delay"
                        )
                        time.sleep(
                            min(_DRIVER_STARTUP_RETRY_DELAY, remaining)
                        )
                        continue
                    raise AndroidOperationalError(
                        f"Maestro action execution failed: {error}"
                    )
                return
        finally:
            primary_error = sys.exception()
            if flow_path:
                try:
                    flow_path.unlink(missing_ok=True)
                except OSError as error:
                    cleanup_error = AndroidOperationalError(
                        f"Maestro temporary flow cleanup failed: {error}"
                    )
                    if primary_error is None:
                        raise cleanup_error from error
                    primary_error.args = (
                        f"{primary_error}; {cleanup_error}",
                        *primary_error.args[1:],
                    )
                    primary_error.add_note(str(cleanup_error))

    def _tap(
        self,
        target: UIElement,
        observation: UIObservation,
    ) -> None:
        kind, payload = self._tap_command(target)
        if kind == "point":
            point = self._safe_tap_point(target, observation)
            self._tap_point(*point)
            return
        try:
            self._run_flow(payload)
        except RuntimeError as error:
            if not self._is_selector_miss(error):
                raise
            if target.bounds is None:
                raise
            safe_point = self._safe_tap_point(target, observation)
            print(
                logtags.prefix(
                    logtags.MAESTRO,
                    "selector tap failed; retrying via coordinate tap",
                )
            )
            self._tap_point(*safe_point)

    @classmethod
    def _safe_tap_point(
        cls,
        target: UIElement,
        observation: UIObservation,
    ) -> tuple[int, int]:
        bounds = target.bounds
        if bounds is None:
            raise ValueError("Observed target has no tappable bounds")

        elements = {
            element.element_id: element
            for element in observation.elements
        }
        blockers = tuple(
            element
            for element in observation.elements
            if element.element_id != target.element_id
            and element.clickable
            and element.enabled
            and element.bounds is not None
            and not cls._is_related(target, element, elements)
        )
        left, top, right, bottom = bounds
        center = ((left + right) // 2, (top + bottom) // 2)
        if not cls._contains(bounds, center):
            raise AndroidOperationalError(
                "Observed target has invalid tappable bounds"
            )
        if not any(cls._contains(element.bounds, center) for element in blockers):
            return center

        x_mid = center[0]
        y_margin = max(1, (bottom - top) // 10)
        x_margin = max(1, (right - left) // 10)
        candidates = (
            (x_mid, top + y_margin),
            (left + x_margin, (top + bottom) // 2),
            (right - x_margin, (top + bottom) // 2),
            (x_mid, bottom - y_margin),
        )
        safe_point = next(
            (
                point
                for point in candidates
                if cls._contains(bounds, point)
                and not any(
                    cls._contains(element.bounds, point)
                    for element in blockers
                )
            ),
            None,
        )
        if safe_point is None:
            raise AndroidOperationalError(
                "Observed target has no unobstructed tap point"
            )
        return safe_point

    @staticmethod
    def _contains(
        bounds: tuple[int, int, int, int],
        point: tuple[int, int],
    ) -> bool:
        left, top, right, bottom = bounds
        x, y = point
        return left <= x < right and top <= y < bottom

    @staticmethod
    def _is_related(
        first: UIElement,
        second: UIElement,
        elements: dict[str, UIElement],
    ) -> bool:
        def has_ancestor(element: UIElement, ancestor_id: str) -> bool:
            parent_id = element.parent_id
            while parent_id is not None:
                if parent_id == ancestor_id:
                    return True
                parent = elements.get(parent_id)
                parent_id = parent.parent_id if parent is not None else None
            return False

        return has_ancestor(first, second.element_id) or has_ancestor(
            second,
            first.element_id,
        )

    @staticmethod
    def _is_selector_miss(error: RuntimeError) -> bool:
        detail = str(error).casefold()
        return any(
            marker in detail
            for marker in (
                "element not found",
                "no visible element",
                "unable to find",
            )
        )

    def _input_text(
        self,
        action: Action,
        target: UIElement,
        secret: str | None,
    ) -> CredentialInputMethod | None:
        use_secret = secret is not None
        replace_existing = action.credential_kind is not None or use_secret

        kind, payload = self._tap_command(target)
        if not replace_existing:
            if kind == "point":
                self._tap_point(*payload)
            else:
                self._run_flow(payload)
            self._run_flow(f"- inputText: {json.dumps(action.input_text)}\n")
            return

        if action.credential_kind is not None:
            target = self._revalidate_credential_target(
                action.credential_kind, target
            )

        if (
            action.credential_kind == CredentialKind.USERNAME
            and secret is not None
            and self._input_username_via_adb(
                target,
                secret,
                action.credential_kind,
            )
        ):
            return CredentialInputMethod.ADB

        self._input_credential_via_clipboard(
            target,
            secret,
            action.credential_kind,
        )
        return CredentialInputMethod.CLIPBOARD

    def _assert_credential_release(self, timeout: float) -> None:
        if self._credential_release_check is None:
            raise AndroidOperationalError(
                "credential entry blocked because no APK trust guard is configured"
            )
        self._credential_release_check(timeout)

    def _revalidate_credential_target(
        self,
        kind: CredentialKind,
        target: UIElement,
    ) -> UIElement:
        self._assert_credential_release(15)
        if self._credential_observer is None:
            raise AndroidOperationalError(
                "credential entry blocked because no fresh UI observer is configured"
            )
        observation = self._credential_observer.reobserve()
        matches = [
            element
            for element in observation.elements
            if element.package_name == self._app_id
            and self._same_credential_target(target, element)
            and infer_credential_kind(
                element.resource_id,
                element.hint_text,
                element.class_name,
                element.accessibility_text,
            )
            == kind
        ]
        if len(matches) != 1:
            raise AndroidOperationalError(
                "credential entry blocked because the trusted target field changed"
            )
        return matches[0]

    @staticmethod
    def _same_credential_target(
        original: UIElement, current: UIElement
    ) -> bool:
        if original.resource_id:
            return original.resource_id == current.resource_id
        return (
            original.class_name == current.class_name
            and original.hint_text == current.hint_text
            and original.accessibility_text == current.accessibility_text
            and original.bounds == current.bounds
        )

    def _focus_credential_target(self, target: UIElement) -> None:
        kind, payload = self._tap_command(target)
        if kind == "point":
            self._tap_point(*payload, timeout=15)
        else:
            self._run_flow(payload, timeout=30)

    def _input_credential_via_clipboard(
        self,
        target: UIElement,
        secret: str | None,
        credential_kind: CredentialKind | None = None,
    ) -> None:
        """Replace a credential exactly without putting its value in the flow."""
        paste_started = False
        paste_completed = False
        try:
            self._focus_credential_target(target)
            if credential_kind is not None:
                target = self._revalidate_credential_target(
                    credential_kind,
                    target,
                )
            self._clear_focused_credential_field(timeout=15)
            if credential_kind is not None:
                target = self._revalidate_credential_target(
                    credential_kind,
                    target,
                )
            paste_started = True
            self._run_flow(
                f"- setClipboard: ${{{MAESTRO_SECRET_ENV}}}\n"
                "- pasteText\n"
                '- setClipboard: ""\n',
                secret=secret,
                timeout=60,
            )
            paste_completed = True
        finally:
            primary_error = sys.exception()
            if (
                paste_started
                and not paste_completed
                and not self.clear_clipboard()
            ):
                cleanup_error = AndroidOperationalError(
                    "credential paste failed and clipboard cleanup "
                    "could not be confirmed"
                )
                if primary_error is None:
                    raise cleanup_error
                primary_error.args = (
                    f"{primary_error}; {cleanup_error}",
                    *primary_error.args[1:],
                )
                primary_error.add_note(str(cleanup_error))

    def clear_clipboard(self) -> bool:
        """Best-effort removal of clipboard contents without exposing them."""
        try:
            self._run_flow('- setClipboard: ""\n', timeout=15)
            return True
        except (AndroidOperationalError, OSError):
            return self._clear_clipboard_via_adb()

    def _clear_clipboard_via_adb(self) -> bool:
        try:
            result = self._run_adb_checked(
                ["shell", "cmd", "clipboard", "set", "text", ""],
                timeout=15,
                operation="clear clipboard via adb",
            )
        except (AndroidOperationalError, OSError):
            return False
        return result.returncode == 0

    def _input_username_via_adb(
        self,
        target: UIElement,
        username: str,
        credential_kind: CredentialKind | None = None,
    ) -> bool:
        """Replace a printable ASCII username through one adb process."""
        center = target.center
        if center is None or not _ADB_SAFE_USERNAME.fullmatch(username):
            return False
        x, y = center
        self._tap_point(x, y, timeout=15)
        if credential_kind is not None:
            self._revalidate_credential_target(
                credential_kind,
                target,
            )
        commands = [
            "input keycombination 113 29",
            "input keyevent 67",
        ]
        for character in username:
            commands.extend(
                (
                    f"input text {shlex.quote(character)}",
                    f"sleep {_ADB_USERNAME_CHARACTER_DELAY_SECONDS}",
                )
            )
        script = "\n".join(commands) + "\n"
        if credential_kind is not None:
            self._revalidate_credential_target(
                credential_kind,
                target,
            )
        try:
            result = subprocess.run(
                ["adb", "-s", self._device_id, "shell", "sh"],
                input=script,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise AndroidOperationalError(
                f"adb username input failed: {error}"
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").replace(
                username,
                "***",
            ).strip()
            raise AndroidOperationalError(
                "adb username input failed"
                + (f": {detail}" if detail else "")
            )
        return True

    def _clear_focused_credential_field(
        self, timeout: float = 180
    ) -> None:
        """Clear the field already focused through a selector-only flow."""
        self._run_adb_checked(
            [
                "shell",
                "sh",
                "-c",
                "input keycombination 113 29 && input keyevent 67",
            ],
            timeout=timeout,
            operation="clear focused credential field",
        )

    def _tap_point(
        self, x: int, y: int, timeout: float = 180
    ) -> None:
        """Deliver a coordinate tap through adb."""
        self._run_adb_checked(
            ["shell", "input", "tap", str(x), str(y)],
            timeout=timeout,
            operation="coordinate tap",
        )

    @classmethod
    def _tap_command(cls, target: UIElement) -> "tuple[str, object]":
        """Resolve the tap command for a target."""
        if target.resource_id:
            return ("flow", f"- tapOn:\n    id: {json.dumps(target.resource_id)}\n")
        own_text = target.own_text
        if own_text:
            return ("flow", f"- tapOn:\n    text: {json.dumps(own_text)}\n")
        center = target.center
        if center is not None:
            return ("point", center)
        if target.label:
            return ("flow", f"- tapOn:\n    text: {json.dumps(target.label)}\n")
        raise ValueError("Observed target has no safe Maestro selector")
