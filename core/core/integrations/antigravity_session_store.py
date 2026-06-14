from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ACTIVE_INTERACTIVE_SESSION_STATES = {
    "starting",
    "running",
    "waiting_browser",
    "waiting_code",
    "login_pending",
    "pending_verification",
}


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

    def session_root(self) -> Path:
        return self.state_dir / "bridge_sessions"

    def session_dir(self, session_id: str) -> Path:
        return self.session_root() / str(session_id)

    def session_status_file(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "status.json"

    def session_input_file(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "input.txt"

    def session_transcript_file(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "transcript.log"

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

    def _save_interactive_session_status(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._default_interactive_session(session_id)
        state.update(payload)
        state["session_id"] = session_id
        state["updated_at"] = self._now().isoformat()
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        self.session_status_file(session_id).write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
        return state

    def load_interactive_session(self, session_id: str | None = None) -> dict[str, Any]:
        target_session_id = str(session_id or self.load().get("interactive_session_id") or "").strip()
        if not target_session_id:
            return self._default_interactive_session("")
        path = self.session_status_file(target_session_id)
        if not path.exists():
            state = self._default_interactive_session(target_session_id)
            state["exists"] = False
            return state
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return self._default_interactive_session(target_session_id)
        state = self._default_interactive_session(target_session_id)
        if isinstance(payload, dict):
            state.update(payload)
        state["exists"] = True
        return state

    def update_interactive_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        current = self.load_interactive_session(session_id)
        current.update(fields)
        if "last_event_at" not in current or not current.get("last_event_at"):
            current["last_event_at"] = self._now().isoformat()
        current["transcript_path"] = str(self.session_transcript_file(session_id))
        saved = self._save_interactive_session_status(session_id, current)
        root = self.load()
        root.update(
            {
                "interactive_session_id": session_id,
                "interactive_session_state": saved.get("state", "idle"),
                "interactive_session_owner": saved.get("owner", "AntigravityManager"),
                "interactive_session_started_at": saved.get("started_at", ""),
                "interactive_session_last_event_at": saved.get("last_event_at", ""),
                "interactive_session_control_mode": saved.get("control_mode", "bridge"),
            }
        )
        self.save(root)
        return saved

    def start_interactive_session(
        self,
        session_id: str,
        *,
        owner: str = "AntigravityManager",
        control_mode: str = "bridge",
        browser_url: str = "",
        log_path: str = "",
    ) -> dict[str, Any]:
        payload = self._default_interactive_session(session_id)
        payload.update(
            {
                "owner": owner,
                "control_mode": control_mode,
                "state": "starting",
                "started_at": self._now().isoformat(),
                "last_event_at": self._now().isoformat(),
                "browser_url": str(browser_url or ""),
                "log_path": str(log_path or ""),
                "transcript_path": str(self.session_transcript_file(session_id)),
                "exists": True,
            }
        )
        payload.pop("session_id", None)
        return self.update_interactive_session(session_id, **payload)

    def finish_interactive_session(self, session_id: str, *, state: str, message: str = "") -> dict[str, Any]:
        saved = self.update_interactive_session(
            session_id,
            state=state,
            message=str(message or ""),
            finished_at=self._now().isoformat(),
            last_event_at=self._now().isoformat(),
            user_input_required=False,
            pending_input=False,
        )
        root = self.load()
        if str(root.get("interactive_session_id") or "") == str(session_id):
            root["interactive_session_state"] = state
            root["interactive_session_last_event_at"] = saved.get("last_event_at", "")
            self.save(root)
        return saved

    def interactive_session_active(self, *, session_id: str | None = None) -> bool:
        session = self.load_interactive_session(session_id)
        return str(session.get("state") or "") in ACTIVE_INTERACTIVE_SESSION_STATES

    def append_interactive_input(self, session_id: str, text: str) -> dict[str, Any]:
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        with self.session_input_file(session_id).open("a", encoding="utf-8") as handle:
            handle.write(str(text))
            if not str(text).endswith("\n"):
                handle.write("\n")
        return self.update_interactive_session(
            session_id,
            last_input_at=self._now().isoformat(),
            last_event_at=self._now().isoformat(),
            pending_input=True,
        )

    def append_interactive_transcript(self, session_id: str, text: str) -> dict[str, Any]:
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        with self.session_transcript_file(session_id).open("a", encoding="utf-8") as handle:
            handle.write(str(text))
        return self.update_interactive_session(session_id, last_event_at=self._now().isoformat())

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

    def record_login_started(self, *, browser_url: str = "") -> dict[str, Any]:
        state = self.load()
        state["last_login_started_at"] = self._now().isoformat()
        if browser_url:
            state["last_browser_opened_at"] = self._now().isoformat()
            state["last_browser_url"] = str(browser_url)
        return self.save(state)

    def record_browser_opened(self, browser_url: str) -> dict[str, Any]:
        state = self.load()
        state["last_browser_opened_at"] = self._now().isoformat()
        state["last_browser_url"] = str(browser_url or "")
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

    def login_started_cooldown_active(self, *, cooldown_sec: int = 300) -> bool:
        state = self.load()
        stamp = self._parse_ts(state.get("last_login_started_at"))
        if stamp is None:
            return False
        return (self._now() - stamp) <= timedelta(seconds=max(60, int(cooldown_sec)))

    def browser_open_cooldown_active(self, *, cooldown_sec: int = 300, browser_url: str = "") -> bool:
        state = self.load()
        if browser_url and str(state.get("last_browser_url") or "") and str(state.get("last_browser_url")) != str(browser_url):
            return False
        stamp = self._parse_ts(state.get("last_browser_opened_at"))
        if stamp is None:
            return False
        return (self._now() - stamp) <= timedelta(seconds=max(60, int(cooldown_sec)))

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

    def login_started_age_sec(self) -> float | None:
        state = self.load()
        stamp = self._parse_ts(state.get("last_login_started_at"))
        if stamp is None:
            return None
        return max(0.0, (self._now() - stamp).total_seconds())

    def interactive_session_age_sec(self) -> float | None:
        state = self.load()
        stamp = self._parse_ts(state.get("interactive_session_started_at"))
        if stamp is None:
            return None
        return max(0.0, (self._now() - stamp).total_seconds())

    def browser_open_age_sec(self) -> float | None:
        state = self.load()
        stamp = self._parse_ts(state.get("last_browser_opened_at"))
        if stamp is None:
            return None
        return max(0.0, (self._now() - stamp).total_seconds())

    def login_failure_age_sec(self) -> float | None:
        state = self.load()
        stamp = self._parse_ts(state.get("last_login_failure_at"))
        if stamp is None:
            return None
        return max(0.0, (self._now() - stamp).total_seconds())

    def _default_interactive_session(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": str(session_id or ""),
            "state": "idle",
            "owner": "AntigravityManager",
            "control_mode": "bridge",
            "started_at": "",
            "finished_at": "",
            "last_event_at": "",
            "last_input_at": "",
            "last_prompt": "",
            "browser_url": "",
            "last_code_hint": "",
            "input_hint": "",
            "message": "",
            "pid": 0,
            "log_path": "",
            "transcript_path": str(self.session_transcript_file(session_id)) if session_id else "",
            "pending_input": False,
            "user_input_required": False,
            "exists": False,
            "updated_at": "",
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "auth_mode": "agy_oauth",
            "models": [],
            "last_success_at": "",
            "last_error": "",
            "last_failure_kind": "",
            "last_login_started_at": "",
            "last_browser_opened_at": "",
            "last_browser_url": "",
            "interactive_session_id": "",
            "interactive_session_state": "idle",
            "interactive_session_owner": "",
            "interactive_session_started_at": "",
            "interactive_session_last_event_at": "",
            "interactive_session_control_mode": "bridge",
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
