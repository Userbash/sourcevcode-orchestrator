from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class MemoryWorkflowStateStore:
    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.session_states: dict[tuple[str, str], dict[str, Any]] = {}
        self.invalidations: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def save_workflow(self, workflow_id: str, state: dict[str, Any]) -> None:
        self.workflows[workflow_id] = dict(state)

    def append_event(self, workflow_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.events.setdefault(workflow_id, []).append({"event_type": event_type, "payload": payload})

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return self.workflows.get(workflow_id)

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
        current = self.session_states.get(key)
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
        }
        self.session_states[key] = snapshot
        return dict(snapshot)

    def get_session_state(self, session_id: str, *, branch: str = "root") -> dict[str, Any] | None:
        state = self.session_states.get((str(session_id), str(branch)))
        return dict(state) if state else None

    def record_invalidation(self, session_id: str, *, reason: str, branch: str = "root", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "session_id": str(session_id),
            "branch": str(branch),
            "reason": str(reason),
            "payload": dict(payload or {}),
            "logged_at": datetime.now(UTC).isoformat(),
        }
        self.invalidations.setdefault((str(session_id), str(branch)), []).append(event)
        return dict(event)

    def recent_invalidations(self, session_id: str, *, branch: str = "root", limit: int = 20) -> list[dict[str, Any]]:
        rows = self.invalidations.get((str(session_id), str(branch)), [])
        return [dict(row) for row in rows[-max(1, int(limit)):]]
