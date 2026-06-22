from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class PostgresStateStore:
    def __init__(self) -> None:
        self.enabled = os.getenv("AI_BRIDGE_ENABLE_POSTGRES_STATE", "false").lower() in {"1", "true", "yes", "on"}
        self.shadow = True
        self._shadow_cache: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._session_states: dict[tuple[str, str], dict[str, Any]] = {}
        self._invalidations: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._storage_path = self._resolve_storage_path()
        self._load()

    def _resolve_storage_path(self) -> Path:
        explicit = os.getenv("AI_BRIDGE_POSTGRES_STATE_PATH", "").strip()
        if explicit:
            path = Path(explicit)
        else:
            base = Path(os.getenv("AI_BRIDGE_MEMORY_STORE_DIR", "").strip() or (Path.cwd() / "memory_store"))
            path = base / "postgres_state_shadow.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _storage_mode(self) -> str:
        return "postgres_shadow_file" if self.enabled else "shadow_file"

    def _key(self, session_id: str, branch: str) -> str:
        return f"{session_id}::{branch}"

    def _split_key(self, raw: str) -> tuple[str, str]:
        session_id, branch = raw.split("::", 1)
        return session_id, branch

    def _load(self) -> None:
        if not self._storage_path.exists():
            return
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return
        workflows = payload.get("workflows")
        events = payload.get("events")
        session_states = payload.get("session_states")
        invalidations = payload.get("invalidations")
        if isinstance(workflows, dict):
            self._shadow_cache = {str(key): dict(value) for key, value in workflows.items() if isinstance(value, dict)}
        if isinstance(events, dict):
            self._events = {str(key): [dict(item) for item in value if isinstance(item, dict)] for key, value in events.items() if isinstance(value, list)}
        if isinstance(session_states, dict):
            self._session_states = {self._split_key(str(key)): dict(value) for key, value in session_states.items() if isinstance(value, dict) and "::" in str(key)}
        if isinstance(invalidations, dict):
            self._invalidations = {self._split_key(str(key)): [dict(item) for item in value if isinstance(item, dict)] for key, value in invalidations.items() if isinstance(value, list) and "::" in str(key)}

    def _persist(self) -> None:
        payload = {
            "workflows": self._shadow_cache,
            "events": self._events,
            "session_states": {self._key(session_id, branch): value for (session_id, branch), value in self._session_states.items()},
            "invalidations": {self._key(session_id, branch): value for (session_id, branch), value in self._invalidations.items()},
        }
        tmp = self._storage_path.with_suffix(self._storage_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._storage_path)

    def save_workflow(self, workflow_id: str, state: dict[str, Any]) -> None:
        self._shadow_cache[str(workflow_id)] = dict(state)
        self._persist()

    def append_event(self, workflow_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._events.setdefault(str(workflow_id), []).append({"event_type": event_type, "payload": dict(payload)})
        self._persist()

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        data = self._shadow_cache.get(str(workflow_id))
        return dict(data) if data else None

    def save_session_state(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        branch: str = "root",
        prompt_version: str = "v1",
        context_version: str = "v1",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        key = (str(session_id), str(branch))
        current = self._session_states.get(key)
        current_version = int(current.get("version", 0) or 0) if current else 0
        if expected_version is not None and expected_version != current_version:
            raise ValueError(f"version conflict for session {session_id}:{branch}; expected {expected_version}, actual {current_version}")
        snapshot = {
            "session_id": str(session_id),
            "branch": str(branch),
            "version": current_version + 1,
            "prompt_version": str(prompt_version),
            "context_version": str(context_version),
            "state": dict(state),
            "updated_at": datetime.now(UTC).isoformat(),
            "storage_mode": self._storage_mode(),
        }
        self._session_states[key] = snapshot
        self._persist()
        return dict(snapshot)

    def get_session_state(self, session_id: str, *, branch: str = "root") -> dict[str, Any] | None:
        state = self._session_states.get((str(session_id), str(branch)))
        return dict(state) if state else None

    def record_invalidation(self, session_id: str, *, reason: str, branch: str = "root", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "session_id": str(session_id),
            "branch": str(branch),
            "reason": str(reason),
            "payload": dict(payload or {}),
            "logged_at": datetime.now(UTC).isoformat(),
            "storage_mode": self._storage_mode(),
        }
        self._invalidations.setdefault((str(session_id), str(branch)), []).append(event)
        self._persist()
        return dict(event)

    def recent_invalidations(self, session_id: str, *, branch: str = "root", limit: int = 20) -> list[dict[str, Any]]:
        rows = self._invalidations.get((str(session_id), str(branch)), [])
        return [dict(row) for row in rows[-max(1, int(limit)):]]
