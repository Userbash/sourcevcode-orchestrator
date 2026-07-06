from __future__ import annotations

from typing import Any

from pydantic import Field

from .models import CompatModel, TaskStatus


class MergeConflictReport(CompatModel):
    has_conflicts: bool = False
    overlapping_files: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ShardExecutionResult(CompatModel):
    workflow_id: str
    task_id: str
    agent_id: str
    status: TaskStatus
    files_changed: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    diff: str = ""
    errors: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MergedWorkflowResult(CompatModel):
    workflow_id: str
    task_id: str
    agent_id: str
    status: TaskStatus
    files_changed: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    merged_diff: str = ""
    errors: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    conflict_report: MergeConflictReport = Field(default_factory=MergeConflictReport)
    received_shards: int = 0
    expected_shards: int = 0
