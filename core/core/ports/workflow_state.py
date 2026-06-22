from __future__ import annotations

from typing import Protocol, TypedDict


class WorkflowState(TypedDict):
    session_id: str
    branch: str
    version: int
    prompt_version: str
    context_version: str
    state: dict[str, object]
    updated_at: str
    storage_mode: str


class WorkflowInvalidation(TypedDict):
    session_id: str
    branch: str
    reason: str
    payload: dict[str, object]
    logged_at: str
    storage_mode: str


class WorkflowStateStore(Protocol):
    def save_workflow(self, workflow_id: str, state: dict[str, object]) -> None: ...
    def append_event(self, workflow_id: str, event_type: str, payload: dict[str, object]) -> None: ...
    def get_workflow(self, workflow_id: str) -> dict[str, object] | None: ...
    def save_session_state(
        self,
        session_id: str,
        state: dict[str, object],
        *,
        branch: str = "root",
        prompt_version: str = "v1",
        context_version: str = "v1",
        expected_version: int | None = None,
    ) -> WorkflowState: ...
    def get_session_state(self, session_id: str, *, branch: str = "root") -> WorkflowState | None: ...
    def record_invalidation(
        self,
        session_id: str,
        *,
        reason: str,
        branch: str = "root",
        payload: dict[str, object] | None = None,
    ) -> WorkflowInvalidation: ...
    def recent_invalidations(self, session_id: str, *, branch: str = "root", limit: int = 20) -> list[WorkflowInvalidation]: ...
