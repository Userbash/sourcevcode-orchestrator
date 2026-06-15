from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .model_selector import ModelChoice
from .models import Priority, Task, TaskType, Complexity


@dataclass(slots=True)
class ProviderState:
    exhausted_until: datetime | None = None
    failures: int = 0
    quota_failures: int = 0
    last_error_type: str = ""
    last_error_detail: str = ""
    last_model_name: str = ""

    @property
    def exhausted(self) -> bool:
        if self.exhausted_until is None:
            return False
        return datetime.now(UTC) < self.exhausted_until


class ProviderBudgetRouter:
    """Global provider fallback router (separate from Antigravity intra-model token router)."""

    def __init__(self) -> None:
        self._session_provider_state: dict[str, dict[str, ProviderState]] = defaultdict(dict)
        self._global_provider_suppression: dict[str, dict[str, Any]] = {}
        self.force_antigravity = os.getenv("AI_BRIDGE_FORCE_ANTIGRAVITY", os.getenv("AI_BRIDGE_FORCE_GEMINI", "false")).strip().lower() in {"1", "true", "yes", "on"}
        self.recovery_timeout_min = int(os.getenv("AI_BRIDGE_RECOVERY_TIMEOUT_MIN", "5"))
        self.policy_mode = os.getenv("AI_BRIDGE_POLICY_MODE", "legacy").strip().lower()

    @staticmethod
    def _session_id(task: Task) -> str:
        return task.session_id or "default"

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        p = provider.strip().lower()
        if p in {"antigravity_api", "antigravity_agy"}:
            return p
        if p in {"antigravity", "antigravity-cli", "agy", "google", "gemini", "gemini-cli"}:
            return "antigravity"
        return p

    def _state(self, task: Task, provider: str) -> ProviderState:
        sid = self._session_id(task)
        key = self._normalize_provider(provider)
        state = self._session_provider_state[sid].get(key)
        if state is None:
            state = ProviderState()
            self._session_provider_state[sid][key] = state
        return state

    @staticmethod
    def _retry_after_seconds(detail: str) -> int | None:
        text = str(detail or "")
        for pattern in (r'retryDelay[\'"]?\s*:\s*[\'"]?(\d+)s', r'Please retry in\s+([0-9]+(?:\.[0-9]+)?)s', r'retry in\s+([0-9]+(?:\.[0-9]+)?)s'):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                return max(1, int(float(match.group(1))))
            except Exception:
                continue
        return None

    def mark_failure(self, task: Task, provider: str, error_type: str, *, detail: str = "", model_name: str = "") -> None:
        state = self._state(task, provider)
        state.failures += 1
        state.last_error_type = str(error_type or "")
        state.last_error_detail = str(detail or "")
        state.last_model_name = str(model_name or "")
        if error_type == "quota_exhaustion":
            state.quota_failures += 1
            retry_after = self._retry_after_seconds(detail) or 0
            cooldown_sec = max(self.recovery_timeout_min * 60, retry_after, min(3600, 45 * max(1, state.quota_failures)))
            state.exhausted_until = datetime.now(UTC) + timedelta(seconds=cooldown_sec)
        elif error_type == "auth_fail":
            state.exhausted_until = datetime.now(UTC) + timedelta(minutes=max(self.recovery_timeout_min, 15))
        elif error_type in {"api_timeout", "tcp_timeout", "sdk_hang"} and state.failures >= 3:
            state.exhausted_until = datetime.now(UTC) + timedelta(seconds=min(300, 20 * state.failures))

    def register_success(self, task: Task, provider: str) -> None:
        state = self._state(task, provider)
        state.failures = 0
        state.exhausted_until = None

    def suppress_provider(self, provider: str, *, minutes: int | None = None, seconds: int | None = None, reason: str) -> None:
        key = self._normalize_provider(provider)
        if seconds is not None:
            ttl_seconds = max(1, int(seconds))
        else:
            ttl_seconds = max(60, int((minutes if minutes is not None else 1) * 60))
        until = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        self._global_provider_suppression[key] = {
            "until": until,
            "reason": reason,
            "suppressed_at": datetime.now(UTC).isoformat(),
            "ttl_seconds": ttl_seconds,
        }

    def release_provider(self, provider: str) -> None:
        key = self._normalize_provider(provider)
        self._global_provider_suppression.pop(key, None)

    def suppression_snapshot(self) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        expired: list[str] = []
        now = datetime.now(UTC)
        for provider, payload in self._global_provider_suppression.items():
            until = payload.get("until")
            if isinstance(until, datetime) and now >= until:
                expired.append(provider)
                continue
            seconds_remaining = None
            if isinstance(until, datetime):
                seconds_remaining = max(0, int((until - now).total_seconds()))
            snapshot[provider] = {
                "until": until.isoformat() if isinstance(until, datetime) else None,
                "reason": payload.get("reason"),
                "suppressed_at": payload.get("suppressed_at"),
                "ttl_seconds": payload.get("ttl_seconds"),
                "seconds_remaining": seconds_remaining,
            }
        for provider in expired:
            self._global_provider_suppression.pop(provider, None)
        return snapshot

    def _provider_globally_suppressed(self, provider: str) -> bool:
        key = self._normalize_provider(provider)
        return key in self.suppression_snapshot()

    @staticmethod
    def _normalized_text_profile(task: Task) -> dict[str, Any]:
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        profile = hints.get("normalized_text_profile")
        return profile if isinstance(profile, dict) else {}

    def preferred_providers(self, task: Task, choice: ModelChoice) -> list[str]:
        preferred_hint = str((task.routing_hints or {}).get("provider_preference") or "").strip()
        preferred = self._normalize_provider(preferred_hint or choice.provider)
        choice_complexity = getattr(choice, "complexity", task.complexity)
        cost_tier = str((task.routing_hints or {}).get("cost_tier") or "").strip().lower()
        source = str((task.routing_hints or {}).get("source") or "").strip().lower()

        profile = self._normalized_text_profile(task)
        profile_risk = str(profile.get("risk_bucket") or "").strip().lower()
        profile_quality = str(profile.get("input_quality_bucket") or "").strip().lower()
        profile_execution = str(profile.get("execution_shape") or "").strip().lower()
        profile_intent = str(profile.get("intent_bucket") or "").strip().lower()
        trusted_profile = str(profile.get("decision_trust") or "").strip().lower() == "trusted"

        is_critical = task.priority in {Priority.CRITICAL} or choice_complexity == Complexity.CRITICAL
        is_high_risk = task.priority in {Priority.HIGH, Priority.CRITICAL} or choice_complexity in {Complexity.HIGH, Complexity.CRITICAL}
        if trusted_profile and profile_risk == "high":
            is_high_risk = True

        if is_critical:
            # Security-critical uses OpenAI first when selected/available; otherwise honor the selector's ready fallback.
            if preferred == "openai":
                base = ["openai", "antigravity", "mistral", "local"]
            else:
                base = [preferred, "antigravity", "mistral", "local", "openai"]
        elif self.policy_mode == "strict":
            # Strict mode still honors an explicit Mistral-first selection for normal engineering work.
            if task.type in {TaskType.CODE, TaskType.REVIEW}:
                if preferred == "mistral":
                    base = ["mistral", "antigravity", "local", "openai"]
                else:
                    base = ["antigravity", "mistral", "local", "openai"]
            elif task.type in {TaskType.TEST, TaskType.FIX}:
                base = ["mistral", "antigravity", "local", "openai"]
            else:
                base = [preferred, "antigravity", "mistral", "local", "openai"]
        elif self.force_antigravity and task.type in {TaskType.CODE, TaskType.REVIEW, TaskType.TEST, TaskType.DOCS, TaskType.RESEARCH, TaskType.FIX}:
            if preferred == "mistral" and task.type in {TaskType.CODE, TaskType.REVIEW, TaskType.TEST, TaskType.FIX}:
                base = ["mistral", "antigravity", "local", "openai"]
            else:
                base = ["antigravity", "mistral", "local", "openai"]
        elif source == "websocket" and cost_tier == "economy":
            if preferred in {"local", "mistral", "antigravity"}:
                base = [preferred, "local", "mistral", "antigravity", "openai"]
            else:
                base = ["local", "mistral", "antigravity", preferred, "openai"]
        elif source == "websocket" and cost_tier == "premium":
            if preferred == "openai":
                base = ["openai", "antigravity", "mistral", "local"]
            else:
                base = [preferred, "openai", "antigravity", "mistral", "local"]
        elif trusted_profile and profile_risk == "high":
            base = ["openai", "antigravity", "mistral", "local", preferred]
        elif profile_quality in {"noisy_but_usable", "sparse"}:
            base = ["openai", "antigravity", "local", "mistral", preferred]
        elif profile_execution == "single_lane_validation" or profile_intent in {"review", "test"}:
            base = ["openai", "mistral", "antigravity", "local", preferred]
        elif task.type in {TaskType.CODE, TaskType.REVIEW}:
            if preferred == "mistral":
                base = ["mistral", "antigravity", "local", "openai"]
            else:
                base = [preferred, "antigravity", "mistral", "local", "openai"]
        elif task.type in {TaskType.TEST, TaskType.FIX}:
            base = ["mistral", "antigravity", "local", "openai"]
        elif task.type in {TaskType.DOCS, TaskType.RESEARCH}:
            base = [preferred, "antigravity", "mistral", "local", "openai"]
        elif is_high_risk:
            base = ["openai", "antigravity", preferred, "mistral", "local"]
        else:
            base = [preferred, "antigravity", "mistral", "local", "openai"]

        seen: set[str] = set()
        ranked: list[str] = []
        for p in base:
            norm = self._normalize_provider(p)
            if norm in seen:
                continue
            seen.add(norm)
            state = self._state(task, norm)
            if state.exhausted or self._provider_globally_suppressed(norm):
                continue
            ranked.append(norm)
        return ranked or ["antigravity", "mistral", "local", "openai"]
