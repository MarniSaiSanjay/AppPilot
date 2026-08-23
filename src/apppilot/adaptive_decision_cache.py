"""Reusable success-gated decision learning and replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import logtags
from .brain import DecisionRequest, ModelDecision, ModelDecisionProvider
from .models import Action, UIElement


_CACHE_VERSION = 1
_DEFAULT_MAX_CACHE_ENTRIES = 200
_CACHE_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AdaptiveDecisionCache:
    """Learn model decisions from successful runs and replay exact matches.

    Subclasses may add permanent domain-specific decisions through
    ``_seeded_decision``. The reusable core owns hashing, success-gated
    persistence, unique-action replay, and stale fast-path invalidation.
    """

    def __init__(
        self,
        fallback: ModelDecisionProvider,
        *,
        cache_namespace: str,
        cache_path: Path | None = None,
        max_cache_entries: int = _DEFAULT_MAX_CACHE_ENTRIES,
        log_tag: str = "",
        known_reason: str = "Matched a known safe control.",
        cached_reason: str = (
            "Reused a decision from a previous successful run."
        ),
    ) -> None:
        if not _CACHE_NAMESPACE.fullmatch(cache_namespace):
            raise ValueError(
                "cache_namespace must contain lowercase letters, numbers, "
                "and hyphens"
            )
        self._fallback = fallback
        self._cache_namespace = cache_namespace
        self._cache_path = cache_path or self._default_cache_path(
            cache_namespace
        )
        self._max_cache_entries = max(1, max_cache_entries)
        self._log_tag = log_tag
        self._known_reason = known_reason
        self._cached_reason = cached_reason
        self._cache = self._load_cache()
        self._pending: dict[str, str] = {}
        self._last_fast_key: str | None = None

    def begin_run(self) -> None:
        self._pending.clear()
        self._last_fast_key = None

    def finish_run(self, succeeded: bool) -> None:
        if succeeded and self._pending:
            self._cache.update(self._pending)
            while len(self._cache) > self._max_cache_entries:
                self._cache.pop(next(iter(self._cache)))
            self._save_cache()
        self._pending.clear()
        self._last_fast_key = None

    def record_executed(self, action: Action) -> None:
        del action

    def decide(self, request: DecisionRequest) -> ModelDecision:
        key = self._request_key(request)
        if key == self._last_fast_key:
            self._last_fast_key = None
            if key in self._cache:
                del self._cache[key]
                self._save_cache()
            return self._fallback_decision(request, key)

        seeded = self._seeded_decision(request)
        if seeded is not None:
            self._last_fast_key = key
            return ModelDecision(
                action=seeded,
                reason=self._known_reason,
                reobserve_required=False,
            )

        cached_action_hash = self._cache.get(key)
        action_hashes = self._action_hashes(request)
        matching_indexes = [
            index
            for index, action_hash in enumerate(action_hashes)
            if action_hash == cached_action_hash
        ]
        if len(matching_indexes) == 1:
            self._last_fast_key = key
            return ModelDecision(
                action=request.available_actions[matching_indexes[0]],
                reason=self._cached_reason,
                reobserve_required=False,
            )

        self._last_fast_key = None
        return self._fallback_decision(request, key)

    def _seeded_decision(self, request: DecisionRequest) -> Action | None:
        del request
        return None

    def _fallback_decision(
        self,
        request: DecisionRequest,
        key: str,
    ) -> ModelDecision:
        decision = self._fallback.decide(request)
        if decision.action is not None:
            try:
                selected_index = request.available_actions.index(
                    decision.action
                )
                self._pending[key] = self._action_hashes(request)[
                    selected_index
                ]
            except ValueError:
                pass
        return decision

    def _request_key(self, request: DecisionRequest) -> str:
        raw = json.dumps(
            {
                "version": _CACHE_VERSION,
                "namespace": self._cache_namespace,
                "goal": request.goal,
                "guidance": request.guidance,
                "actions": sorted(self._action_hashes(request)),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _action_hashes(
        cls,
        request: DecisionRequest,
    ) -> tuple[str, ...]:
        return tuple(
            cls._action_signature_hash(request, action)
            for action in request.available_actions
        )

    @classmethod
    def _action_signature_hash(
        cls,
        request: DecisionRequest,
        action: Action,
    ) -> str:
        target = request.observation.find(action.target_id)
        signature = (
            action.kind.value,
            (
                action.credential_kind.value
                if action.credential_kind is not None
                else None
            ),
            action.input_text,
            cls._element_text(target) if target is not None else None,
            target.class_name if target is not None else None,
            target.clickable if target is not None else None,
            target.is_input if target is not None else None,
        )
        raw = json.dumps(
            signature,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _element_text(element: UIElement) -> str:
        return " ".join(
            (
                element.resource_id,
                element.text,
                element.accessibility_text,
                element.hint_text,
                element.label,
            )
        ).casefold()

    @staticmethod
    def _default_cache_path(cache_namespace: str) -> Path:
        root = Path(
            os.environ.get(
                "XDG_CACHE_HOME",
                Path.home() / ".cache",
            )
        )
        return (
            root
            / "apppilot"
            / f"{cache_namespace}-decisions-v{_CACHE_VERSION}.json"
        )

    def _load_cache(self) -> dict[str, str]:
        if not self._cache_path.exists():
            return {}
        try:
            payload = json.loads(
                self._cache_path.read_text(encoding="utf-8")
            )
            entries = payload.get("entries", {})
            if payload.get("version") != _CACHE_VERSION or not isinstance(
                entries,
                dict,
            ):
                raise ValueError("unsupported cache format")
            return {
                key: value
                for key, value in entries.items()
                if isinstance(key, str)
                and _SHA256.fullmatch(key)
                and isinstance(value, str)
                and _SHA256.fullmatch(value)
            }
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            logtags.trace(
                f"adaptive decision cache ignored: {error}",
                self._log_tag,
            )
            return {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": _CACHE_VERSION,
                        "entries": self._cache,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._cache_path)
        except (OSError, UnicodeError) as error:
            logtags.trace(
                f"adaptive decision cache was not saved: {error}",
                self._log_tag,
            )
