from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class AntigravitySessionStore:
    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        legacy_state_dir: Path | None = None,
        now_fn: Any | None = None,
    ) -> None:
        self.state_dir = Path(state_dir or (Path.home() / ".gemini" / "antigravity-cli"))
        self.legacy_state_dir = Path(legacy_state_dir or (Path.home() / ".antigravity" / "antigravity-cli"))
        self.state_file = self.state_dir / "session_bridge_state.json"
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def auth_marker_paths(self) -> list[Path]:
        return [
            self.state_dir / "installation_id",
            self.state_dir / "conversations",
            self.state_dir / "cache",
            self.state_dir / "settings.json",
            self.legacy_state_dir / "settings.json",
        ]

    def auth_marker_present(self) -> bool:
        return any(path.exists() for path in self.auth_marker_paths())

    def load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return self._default_state()
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return self._default_state()
        state = self._default_state()
        if isinstance(payload, dict):
            state.update(payload)
        state["auth_marker_present"] = self.auth_marker_present()
        return state

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._default_state()
        state.update(payload)
        state["updated_at"] = self._now().isoformat()
        state["auth_marker_present"] = self.auth_marker_present()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
        return state

    def record_success(self, *, models: list[str] | None = None, auth_mode: str = "agy_oauth") -> dict[str, Any]:
        state = self.load()
        state.update(
            {
                "last_success_at": self._now().isoformat(),
                "last_error": "",
                "last_failure_kind": "",
                "login_failures": 0,
                "last_login_failure_at": "",
                "auth_mode": auth_mode,
                "models": list(models or []),
            }
        )
        return self.save(state)

    def record_failure(self, error: str, *, failure_kind: str) -> dict[str, Any]:
        state = self.load()
        state.update({
            "last_error": str(error or ""),
            "last_failure_kind": str(failure_kind or "unknown"),
        })
        return self.save(state)

    def record_login_failure(self, error: str, *, failure_kind: str = "auth_required") -> dict[str, Any]:
        state = self.load()
        state["login_failures"] = int(state.get("login_failures", 0) or 0) + 1
        state["last_login_failure_at"] = self._now().isoformat()
        state["last_error"] = str(error or "")
        state["last_failure_kind"] = str(failure_kind or "auth_required")
        return self.save(state)

    def recently_verified(self, *, within_sec: int = 43200) -> bool:
        state = self.load()
        stamp = self._parse_ts(state.get("last_success_at"))
        if stamp is None:
            return False
        return (self._now() - stamp) <= timedelta(seconds=max(60, int(within_sec)))

    def login_failure_cooldown_active(self, *, cooldown_sec: int = 900) -> bool:
        state = self.load()
        stamp = self._parse_ts(state.get("last_login_failure_at"))
        if stamp is None:
            return False
        return (self._now() - stamp) <= timedelta(seconds=max(60, int(cooldown_sec)))

    def snapshot(self) -> dict[str, Any]:
        return self.load()

    def success_age_sec(self) -> float | None:
        state = self.load()
        stamp = self._parse_ts(state.get("last_success_at"))
        if stamp is None:
            return None
        return max(0.0, (self._now() - stamp).total_seconds())

    def login_failure_age_sec(self) -> float | None:
        state = self.load()
        stamp = self._parse_ts(state.get("last_login_failure_at"))
        if stamp is None:
            return None
        return max(0.0, (self._now() - stamp).total_seconds())

    def _default_state(self) -> dict[str, Any]:
        return {
            "auth_mode": "agy_oauth",
            "models": [],
            "last_success_at": "",
            "last_error": "",
            "last_failure_kind": "",
            "last_login_failure_at": "",
            "login_failures": 0,
            "updated_at": "",
            "auth_marker_present": self.auth_marker_present(),
        }

    def _now(self) -> datetime:
        value = self._now_fn()
        if isinstance(value, datetime):
            return value
        return datetime.now(UTC)

    @staticmethod
    def _parse_ts(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None
