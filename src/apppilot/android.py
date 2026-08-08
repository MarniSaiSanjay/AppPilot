"""Android/Maestro infrastructure: UI observation and action execution."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from .models import Action, ActionKind, UIElement, UIObservation
from .safety import infer_credential_kind

APP_ID = "com.microsoft.office.officehubrow"


# Env var used to hand a resolved secret to the Maestro subprocess. The
# MAESTRO_ prefix lets Maestro read it from the process environment via
# ${...} interpolation, so the value never appears in the flow YAML, in argv,
# or in logs.
MAESTRO_SECRET_ENV = "MAESTRO_APPPILOT_INPUT_SECRET"

# How many characters to erase from a credential field before entering the
# secret, so repeated entries replace rather than append. Generous upper bound.
CREDENTIAL_FIELD_ERASE_CHARS = 128


class MaestroHierarchyObserver:
    def __init__(self, device_id: str, max_elements: int = 100) -> None:
        self._device_id = device_id
        self._max_elements = max_elements

    def observe(self) -> UIObservation:
        command = [
            "maestro",
            "--no-ansi",
            "--udid",
            self._device_id,
            "hierarchy",
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Maestro hierarchy observation failed: {error}")

        try:
            hierarchy = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Maestro returned an invalid UI hierarchy") from error

        elements: list[UIElement] = []
        self._collect(hierarchy, (), None, False, elements)
        return UIObservation(tuple(elements[: self._max_elements]))

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
        system_ui = in_system_ui or resource_id.startswith("com.android.systemui:")
        text = self._clean(attributes.get("text"))
        accessibility_text = self._clean(attributes.get("accessibilityText"))
        hint_text = self._clean(attributes.get("hintText"))
        class_name = self._clean(attributes.get("class"), limit=120)
        clickable = self._as_bool(attributes.get("clickable", node.get("clickable")))
        enabled = self._as_bool(attributes.get("enabled", node.get("enabled")), True)
        is_input = class_name.endswith("EditText") or "TextInput" in class_name

        # Redact credential fields: once a secret (e.g. a password) is typed, it
        # appears as this field's live text/accessibility value in the hierarchy.
        # Drop those value-bearing fields so the secret never reaches the
        # observation, label, model prompt, or execution trace. The stable hint
        # (e.g. "Password") is kept as a safe descriptor.
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
                )
            )

        return self._unique(own_labels + child_labels)[:8]

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
        commands = self._commands_for(action, observation, secret is not None)
        self._run_flow(commands, secret=secret)

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

    def stop_app(self) -> None:
        """Force-stop (kill) the app."""
        self._run_flow(f"- stopApp: {json.dumps(self._app_id)}\n")

    def _run_flow(
        self,
        commands: str,
        secret: str | None = None,
        timeout: float = 60,
    ) -> None:
        flow = f"appId: {self._app_id}\n---\n{commands}"
        # For credential inputs the secret is never written to the flow file; it
        # is passed to Maestro through a MAESTRO_-prefixed environment variable
        # and interpolated as ${...}. This keeps it out of the YAML, argv and logs.
        run_env: dict[str, str] | None = None
        if secret is not None:
            run_env = {**os.environ, MAESTRO_SECRET_ENV: secret}
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
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
                # Never let a resolved secret surface via an error message, even
                # if Maestro echoes the interpolated value in its output.
                if secret:
                    error = error.replace(secret, "***")
                raise RuntimeError(f"Maestro action execution failed: {error}")
        finally:
            if flow_path:
                flow_path.unlink(missing_ok=True)

    def _commands_for(
        self,
        action: Action,
        observation: UIObservation,
        use_secret: bool,
    ) -> str:
        if action.kind == ActionKind.PRESS_BACK:
            return "- pressKey: BACK\n"

        target = observation.find(action.target_id)
        if target is None:
            raise ValueError("Cannot execute an action without an observed target")
        selector = self._selector(target)

        if action.kind == ActionKind.TAP:
            return f"- tapOn:\n{selector}"
        if action.kind == ActionKind.INPUT_TEXT:
            if action.credential_kind is not None or use_secret:
                # Clear any existing/accumulated content first so repeated entry
                # replaces rather than appends, then inject the secret via the
                # environment placeholder (never the literal value in the YAML).
                return (
                    f"- tapOn:\n{selector}"
                    f"- eraseText: {CREDENTIAL_FIELD_ERASE_CHARS}\n"
                    f"- inputText: ${{{MAESTRO_SECRET_ENV}}}\n"
                )
            value = json.dumps(action.input_text)
            return f"- tapOn:\n{selector}- inputText: {value}\n"
        raise ValueError(f"Unsupported action kind: {action.kind}")

    @staticmethod
    def _selector(target: UIElement) -> str:
        if target.resource_id:
            return f"    id: {json.dumps(target.resource_id)}\n"
        if target.selector_text:
            return f"    text: {json.dumps(target.selector_text)}\n"
        raise ValueError("Observed target has no safe Maestro selector")

