"""Android/Maestro infrastructure: UI observation and action execution."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence

from . import logtags
from .models import Action, ActionKind, UIElement, UIObservation
from .safety import infer_credential_kind

APP_ID = "com.microsoft.office.officehubrow"


# Env var carrying a resolved secret to Maestro. The MAESTRO_ prefix lets Maestro
# read it via ${...} interpolation, so the value never enters the YAML, argv, or logs.
MAESTRO_SECRET_ENV = "MAESTRO_APPPILOT_INPUT_SECRET"

# Characters to erase from a credential field before entry so repeats replace
# rather than append. Generous upper bound.
CREDENTIAL_FIELD_ERASE_CHARS = 128

# Maestro spins up an on-device driver app per invocation; on a busy/slow
# emulator it can miss its startup window. This is an infra flake, not a real
# action failure, so give the driver a longer startup budget and, on timeout,
# reset the adb connection and retry a bounded number of times.
_DRIVER_STARTUP_TIMEOUT_MARKER = "driver did not start up in time"
_DRIVER_STARTUP_MAX_ATTEMPTS = 3
_DRIVER_STARTUP_RETRY_DELAY = 3.0
# Maestro reads this (milliseconds) to size its driver-startup wait; the default
# is often too short on a loaded emulator right after install/uninstall/build.
_DRIVER_STARTUP_TIMEOUT_ENV = "MAESTRO_DRIVER_STARTUP_TIMEOUT"
_DRIVER_STARTUP_TIMEOUT_MS = "120000"


class AndroidOperationalError(RuntimeError):
    """Expected device/Maestro infrastructure failure."""


class MaestroHierarchyObserver:
    # System-UI prefix always excluded from observations. The active IME package
    # is resolved per device and appended in ``observe`` - an open keyboard adds
    # 100+ key nodes that would crowd real controls out of the truncated tree.
    # ``android:id/input_method_`` is the framework IME navigation bar (e.g. the
    # ``input_method_nav_back`` button): it lives under the ``android`` namespace
    # rather than the keyboard package, and toggles with the keyboard - excluding
    # it keeps the screen fingerprint stable across keyboard show/hide so the
    # login loop guards (already-entered field, re-entry, stuck) are not defeated.
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
        self._popup_unblock_budget_limit = max(
            0,
            int(
                os.environ.get("APPPILOT_POPUP_UNBLOCK_BUDGET", "3")
            ),
        )
        self._popup_unblock_budget = self._popup_unblock_budget_limit
        self._popup_unblock_settle_seconds = float(
            os.environ.get("APPPILOT_POPUP_UNBLOCK_SETTLE", "1.0")
        )

    def reset_recovery_budget(self) -> None:
        """Reset bounded blank-popup recovery for a new lifecycle attempt."""
        self._popup_unblock_budget = self._popup_unblock_budget_limit

    def observe(self) -> UIObservation:
        self._ensure_excluded_prefixes()
        observation = self._capture()
        if self._is_blank(observation):
            recovered = self._unblock_blank_screen()
            if recovered is not None:
                observation = recovered
        return observation

    def reobserve(self) -> UIObservation:
        """Capture a fresh observation before executing a model decision."""
        return self.observe()

    def _capture(self) -> UIObservation:
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
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
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
        self._collect(hierarchy, (), None, False, elements)
        return UIObservation(tuple(elements[: self._max_elements]))

    @staticmethod
    def _is_blank(observation: UIObservation) -> bool:
        """True when no observed element carries any usable semantics.

        Mirrors the label/id/clickable/input filter used everywhere else, so a
        "blank" observation is exactly what the evaluator would see as
        ``<no relevant UI elements>``."""
        return not any(
            e.label or e.resource_id or e.clickable or e.is_input
            for e in observation.elements
        )

    def _unblock_blank_screen(self) -> "UIObservation | None":
        """Recover a blank observation caused by a separate focused window.

        Some app screens (e.g. the notification opt-in) fill the display yet host
        their content in a separate window that grabs input focus. The hierarchy
        dump only returns the focused window's tree, so it comes back blank and
        the controls are invisible. When such a window is focused, press BACK to
        return focus to the app's own window and re-capture. Deterministic - no
        AI/OCR/coordinates/screenshots. Returns the recovered observation, or
        None when the guard does not apply."""
        if self._popup_unblock_budget <= 0:
            return None
        if not self._focused_window_is_popup():
            return None
        self._popup_unblock_budget -= 1
        if not self._press_back():
            return None
        time.sleep(self._popup_unblock_settle_seconds)
        return self._capture()

    def _focused_window_is_popup(self) -> bool:
        """True when the focused window is a separate pop-up window not covered
        by the hierarchy dump (read from ``dumpsys window`` mCurrentFocus)."""
        try:
            result = subprocess.run(
                ["adb", "-s", self._device_id, "shell", "dumpsys", "window"],
                check=False, capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return False
        for line in (result.stdout or "").splitlines():
            if "mCurrentFocus" in line and "pop-up window" in line.lower():
                return True
        return False

    def _press_back(self) -> bool:
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
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _ensure_excluded_prefixes(self) -> None:
        """Resolve the active IME package once and add it to the excluded set.

        Generic across keyboards: the current input method is read from the
        device rather than assuming a specific keyboard app. Best-effort - if the
        lookup fails, only the system UI is excluded."""
        if self._ime_resolved:
            return
        self._ime_resolved = True
        try:
            package = (self._ime_provider or self._query_ime_package)()
        except Exception:
            package = None
        if package:
            self._excluded_prefixes = self._excluded_prefixes + (f"{package}:",)

    def _query_ime_package(self) -> "str | None":
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
            timeout=10,
        )
        value = (result.stdout or "").strip()
        if not value or value == "null":
            return None
        # e.g. "com.google.android.inputmethod.latin/com.android...LatinIME".
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
        clickable = self._as_bool(attributes.get("clickable", node.get("clickable")))
        enabled = self._as_bool(attributes.get("enabled", node.get("enabled")), True)
        is_input = class_name.endswith("EditText") or "TextInput" in class_name

        # Safety: drop the live text/accessibility value of a credential field so
        # a typed secret never reaches the observation, prompt, or trace. The
        # stable hint (e.g. "Password") is kept as a safe descriptor.
        field_credential_kind = (
            infer_credential_kind(resource_id, hint_text, class_name, accessibility_text)
            if is_input
            else None
        )
        if field_credential_kind is not None:
            text = ""
            accessibility_text = ""

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
                )
            )

        return [] if system_ui else self._unique(own_labels + child_labels)[:8]

    @staticmethod
    def _parse_bounds(value: object) -> "tuple[int, int, int, int] | None":
        """Parse Maestro/UIAutomator bounds ("[left,top][right,bottom]").

        Used to tap a clickable node by its own centre point when it has no
        stable id/text selector, so the touch lands on the element itself rather
        than a merged descendant that may report zero bounds."""
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
    def _unique(values: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))



class MaestroExecutor:
    def __init__(self, app_id: str, device_id: str) -> None:
        self._app_id = app_id
        self._device_id = device_id

    def execute(
        self,
        action: Action,
        observation: UIObservation,
        secret: str | None = None,
    ) -> None:
        if action.kind == ActionKind.PRESS_BACK:
            self._run_flow("- pressKey: BACK\n")
            return

        target = observation.find(action.target_id)
        if target is None:
            raise ValueError("Cannot execute an action without an observed target")

        if action.kind == ActionKind.TAP:
            self._tap(target)
            return
        if action.kind == ActionKind.INPUT_TEXT:
            self._input_text(action, target, secret)
            return
        raise ValueError(f"Unsupported action kind: {action.kind}")

    def open_link(self, deep_link: str) -> None:
        """Launch an exact deep link deterministically via Maestro ``openLink``.

        The link is executed verbatim as supplied by the test case; nothing about
        it is inferred, modified, or chosen by a model.
        """
        commands = f"- openLink: {json.dumps(deep_link)}\n"
        self._run_flow(commands)

    def launch_app(self) -> None:
        """Launch the app (used e.g. by first-install warm-up)."""
        self._run_flow(f"- launchApp: {json.dumps(self._app_id)}\n")

    def launch_app_via_open_btn_click(self, timeout: float = 60) -> None:
        """Launch the app by tapping the store's "Open" button via Maestro.

        Deterministic text tap - launches the freshly installed app the way a
        user would, instead of an adb CLI launch. Maestro waits for the button to
        appear (so it tolerates the install finishing). No coordinates, OCR, or
        model involved.
        """
        self._run_flow('- tapOn:\n    text: "Open"\n', timeout=timeout)

    def launch_app_via_adb(self, timeout: float = 30) -> None:
        """Launch the app via its default launcher activity (adb monkey LAUNCHER).

        Deterministic CLI launch used by the installed batch to bring the app to
        the foreground. Uses the package's default launcher activity, so no
        activity name needs to be known in advance.
        """
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
        # monkey exits 0 and prints an error line when it cannot find a launchable
        # activity, so treat that as a genuine failure rather than a launch.
        if result.returncode != 0 or "No activities found" in combined:
            raise RuntimeError(
                f"adb launch failed for {self._app_id}: {combined.strip()}"
            )

    def stop_app(self) -> None:
        """Force-stop (kill) the app."""
        self._run_flow(f"- stopApp: {json.dumps(self._app_id)}\n")

    def is_installed(self) -> bool:
        """Return whether the app package is currently installed (via adb)."""
        result = self._run_adb_checked(
            ["shell", "pm", "list", "packages", self._app_id],
            operation="query installed packages",
        )
        expected = f"package:{self._app_id}"
        return expected in {
            line.strip() for line in (result.stdout or "").splitlines()
        }

    def is_foreground(self, timeout: float = 15) -> bool:
        """Return whether the target app package is the foreground (resumed) app.

        Deterministic Android state check via adb ``dumpsys`` - NOT a UI heuristic
        and NOT AI-driven. Used to confirm the app actually came to the foreground
        after the store window's "Open" button is tapped, so a successful tap is
        never mistaken for the app actually launching. A tap can report success
        while the store window (e.g. Google Play) is still foreground; only the
        resumed-activity package proves the app is really open.
        """
        result = self._run_adb_checked(
            ["shell", "dumpsys", "activity", "activities"],
            timeout=timeout,
            operation="query foreground activity",
        )
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            # `mResumedActivity` / `topResumedActivity` name the currently
            # foreground activity; the app is foreground only if its package owns
            # that resumed activity.
            if "ResumedActivity" in stripped and self._app_id in stripped:
                return True
        return False

    def uninstall_app(self) -> None:
        """Uninstall the app package and fail if adb does not confirm success."""
        result = self._run_adb_checked(
            ["uninstall", self._app_id], operation="uninstall app"
        )
        combined = f"{result.stdout or ''}{result.stderr or ''}"
        if "Success" not in combined:
            raise AndroidOperationalError(
                f"adb uninstall did not confirm success: {combined.strip()}"
            )

    def ensure_uninstalled(self) -> bool:
        """Guarantee a genuinely fresh (uninstalled) app before a first-open test.

        Clearing app data is not enough for a true first-install, so this removes
        the APK entirely when present. Returns True iff an app was actually
        uninstalled (False if already absent).
        """
        if self.is_installed():
            self.uninstall_app()
            if self.is_installed():
                raise AndroidOperationalError(
                    f"package {self._app_id} is still installed after uninstall"
                )
            return True
        return False

    def install_apk(self, apk_path: str, timeout: float = 300) -> None:
        """Install a local APK via adb (reinstall if already present).

        Deterministic replacement for tapping the Play Store "Install" button: we
        install the locally built app directly onto the device.
        """
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

    def _run_adb(
        self, args: Sequence[str], timeout: float = 180
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["adb", "-s", self._device_id, *args],
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
        except (OSError, subprocess.SubprocessError) as error:
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

    def _reset_adb_connection(self, timeout: float = 60) -> None:
        """Restart the adb server and wait for the device to reattach.

        The reliable recovery for a stuck Maestro driver: a fresh adb server
        clears the wedged driver connection so the next attempt starts clean.
        Best-effort - failures here are swallowed so the caller can still retry.
        """
        for args in (["kill-server"], ["start-server"]):
            try:
                subprocess.run(
                    ["adb", *args], check=False,
                    capture_output=True, text=True, timeout=timeout,
                )
            except (subprocess.SubprocessError, OSError):
                pass
        try:
            self._run_adb(["wait-for-device"], timeout=timeout)
        except (subprocess.SubprocessError, OSError):
            pass

    def _run_flow(
        self,
        commands: str,
        secret: str | None = None,
        timeout: float = 60,
    ) -> None:
        flow = f"appId: {self._app_id}\n---\n{commands}"
        # Give the Maestro driver a longer startup budget on loaded emulators.
        # For credential inputs the secret is never written to the flow file; it
        # is passed via a MAESTRO_-prefixed env var and interpolated as ${...},
        # keeping it out of the YAML, argv and logs.
        run_env: dict[str, str] = {
            **os.environ,
            _DRIVER_STARTUP_TIMEOUT_ENV: _DRIVER_STARTUP_TIMEOUT_MS,
        }
        if secret is not None:
            run_env[MAESTRO_SECRET_ENV] = secret
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
                        timeout=timeout,
                        env=run_env,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    raise AndroidOperationalError(
                        f"Maestro action execution failed: {error}"
                    ) from error
                if result.returncode != 0:
                    error = result.stderr.strip() or result.stdout.strip()
                    # Never let a resolved secret surface via an error message,
                    # even if Maestro echoes the interpolated value in its output.
                    if secret:
                        error = error.replace(secret, "***")
                    # Retry only the Maestro driver-startup flake; real action
                    # failures still raise on the first attempt.
                    if (
                        _DRIVER_STARTUP_TIMEOUT_MARKER in error.lower()
                        and attempt < _DRIVER_STARTUP_MAX_ATTEMPTS
                    ):
                        print(
                            logtags.prefix(
                                logtags.MAESTRO,
                                f"driver startup timeout "
                                f"(attempt {attempt}/{_DRIVER_STARTUP_MAX_ATTEMPTS}); "
                                "resetting adb and retrying",
                            )
                        )
                        self._reset_adb_connection()
                        time.sleep(_DRIVER_STARTUP_RETRY_DELAY)
                        continue
                    raise AndroidOperationalError(
                        f"Maestro action execution failed: {error}"
                    )
                return
        finally:
            if flow_path:
                flow_path.unlink(missing_ok=True)

    def _tap(self, target: UIElement) -> None:
        kind, payload = self._tap_command(target)
        if kind == "point":
            self._tap_point(*payload)
            return
        try:
            self._run_flow(payload)
        except RuntimeError as error:
            if not self._is_selector_miss(error):
                raise
            # A selector tap (id/text) can miss an element that IS present in the
            # a11y snapshot we observed - Maestro's own lookup can fail to match a
            # Compose node, or the screen is mid-settle when the driver runs. When
            # we already know the element's on-screen centre, fall back to a
            # coordinate tap via adb (the most reliable delivery on Compose)
            # instead of letting one flaky tap crash the whole suite. Without
            # bounds there is no safe fallback, so the error propagates.
            center = target.center
            if center is None:
                raise
            print(
                logtags.prefix(
                    logtags.MAESTRO,
                    "selector tap failed; retrying via coordinate tap",
                )
            )
            self._tap_point(*center)

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
        self, action: Action, target: UIElement, secret: str | None
    ) -> None:
        use_secret = secret is not None
        replace_existing = action.credential_kind is not None or use_secret

        kind, payload = self._tap_command(target)
        # Focus the target field first. Coordinate taps go through adb; text
        # matches run a Maestro tapOn. Either way the field ends up focused so
        # the subsequent clear/type acts on it.
        if kind == "point":
            self._tap_point(*payload)
        else:
            self._run_flow(payload)

        if not replace_existing:
            self._run_flow(f"- inputText: {json.dumps(action.input_text)}\n")
            return

        # Credential / replace entry: empty the field, then RE-FOCUS and type in
        # a single Maestro flow. Each ``maestro test`` runs in its own subprocess
        # and tears down its driver on exit, which drops the soft keyboard and
        # the field's focus - a standalone ``inputText`` then types into nothing,
        # leaving the field empty (seen as an unfilled password entry and an
        # "enter your password" validation error). Re-focusing in the same flow
        # as the type keeps the field focused so the secret always lands. The
        # field is already cleared here, so the re-tap's caret position - the
        # reason the earlier clear moves the caret to the end - no longer matters.
        self._clear_focused_field()
        input_line = f"- inputText: ${{{MAESTRO_SECRET_ENV}}}\n"
        if kind == "point":
            self._tap_point(*payload)
            self._run_flow(input_line, secret=secret)
        else:
            self._run_flow(payload + input_line, secret=secret)

    def _clear_focused_field(self) -> None:
        """Deterministically empty the currently focused text field.

        ``eraseText`` only deletes to the LEFT of the caret, so when the focus
        tap lands in the middle of pre-filled text the characters to its right
        survive - producing a garbled mix of old and new input (observed on the
        sign-in email field). Moving the caret to the end first makes the erase
        remove the whole field regardless of where the tap placed the caret.
        """
        # KEYCODE_MOVE_END (123): caret to end of the focused field, so the
        # generous eraseText below clears everything to its left = the field.
        self._run_adb_checked(
            ["shell", "input", "keyevent", "123"],
            operation="move text cursor",
        )
        self._run_flow(f"- eraseText: {CREDENTIAL_FIELD_ERASE_CHARS}\n")

    def _tap_point(self, x: int, y: int) -> None:
        """Deliver a coordinate tap through adb's input pipeline.

        Maestro's ``tapOn: point`` can silently no-op on Compose surfaces: it
        reports COMPLETED but the synthesized gesture is never registered by the
        app, so nothing advances (observed on the sign-in "Continue with
        Microsoft" button and the store "Open" button). ``adb shell input tap``
        injects a real tap that the app receives, so it is used for every
        coordinate tap.
        """
        self._run_adb_checked(
            ["shell", "input", "tap", str(x), str(y)],
            operation="coordinate tap",
        )

    @classmethod
    def _tap_command(cls, target: UIElement) -> "tuple[str, object]":
        """Resolve the most reliable tap for a target.

        Returns ``("flow", yaml)`` for a Maestro selector-based tap or
        ``("point", (x, y))`` for a coordinate tap delivered via adb.

        Preference order:
        1. ``id`` - a stable resource id is the safest, visibility-checked match.
        2. own ``text`` - only text that belongs to THIS node (see
           UIElement.own_text); a merged descendant label is NOT used here
           because Maestro would resolve it to the child node.
        3. ``point`` - the node's own centre (tapped via adb), so a clickable
           element with no id and no own text (a Compose button whose caption
           lives on a child) is tapped exactly on itself. Maestro's own point
           tap is unreliable on Compose, so this goes through adb.
        4. label ``text`` - last-resort match by the merged label, used only when
           the node has no bounds to compute a centre from.
        """
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
