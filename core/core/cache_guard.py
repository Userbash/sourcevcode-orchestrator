from __future__ import annotations

from enum import Enum
from typing import Any


class GuardAction(str, Enum):
    OK = "ok"
    WARN = "warn"
    SOFT_STOP = "soft_stop"
    HARD_STOP = "hard_stop"


class CacheGuard:
    def __init__(
        self,
        *,
        uncached_threshold: int = 50000,
        cached_threshold: int = 20000,
        hit_rate_threshold: float = 0.20,
    ) -> None:
        self.uncached_threshold = int(uncached_threshold)
        self.cached_threshold = int(cached_threshold)
        self.hit_rate_threshold = float(hit_rate_threshold)
        self._consecutive_misses: dict[str, int] = {}
        self._last_actions: dict[str, str] = {}

    def _is_miss(self, *, uncached_input_tokens: int, cached_input_tokens: int, cache_hit_rate: float | None) -> bool:
        if uncached_input_tokens > self.uncached_threshold and cached_input_tokens < self.cached_threshold:
            return True
        if cache_hit_rate is not None and float(cache_hit_rate) < self.hit_rate_threshold and uncached_input_tokens > 0:
            return True
        return False

    def observe(
        self,
        *,
        session_id: str,
        uncached_input_tokens: int,
        cached_input_tokens: int,
        cache_hit_rate: float | None = None,
    ) -> dict[str, object]:
        session_key = str(session_id or "default")
        miss = self._is_miss(
            uncached_input_tokens=max(0, int(uncached_input_tokens)),
            cached_input_tokens=max(0, int(cached_input_tokens)),
            cache_hit_rate=None if cache_hit_rate is None else float(cache_hit_rate),
        )
        streak = self._consecutive_misses.get(session_key, 0)
        streak = streak + 1 if miss else 0
        self._consecutive_misses[session_key] = streak
        action = GuardAction.OK
        if streak >= 3:
            action = GuardAction.HARD_STOP
        elif streak == 2:
            action = GuardAction.SOFT_STOP
        elif streak == 1:
            action = GuardAction.WARN
        self._last_actions[session_key] = action.value
        return {
            "session_id": session_key,
            "cache_miss": miss,
            "consecutive_misses": streak,
            "action": action.value,
        }

    def snapshot(self, session_id: str) -> dict[str, Any]:
        session_key = str(session_id or "default")
        return {
            "session_id": session_key,
            "consecutive_misses": int(self._consecutive_misses.get(session_key, 0) or 0),
            "action": str(self._last_actions.get(session_key, GuardAction.OK.value)),
            "blocked": self._last_actions.get(session_key) == GuardAction.HARD_STOP.value,
        }
