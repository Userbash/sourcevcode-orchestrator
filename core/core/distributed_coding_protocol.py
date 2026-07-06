from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import Field

from .models import CompatModel


class WorkShardStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class JSONThemes(CompatModel):
    primary: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class CodingWorkShard(CompatModel):
    shard_id: str = Field(default_factory=lambda: str(uuid4()))
    target_agent: str
    target_capability: str = "code"
    queue_name: str
    file_targets: list[str] = Field(default_factory=list)
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    json_themes: JSONThemes = Field(default_factory=JSONThemes)
    status: WorkShardStatus = WorkShardStatus.PENDING
    lane_kind: str = "implement"
    worker_role: str = "core_logic"
    focus_prompt: str = ""


class DistributedCodingTask(CompatModel):
    workflow_id: str = Field(default_factory=lambda: str(uuid4()))
    objective: str
    repo_path: str
    branch: str = "main"
    target_agents: list[str] = Field(default_factory=list)
    file_targets: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    json_themes: JSONThemes = Field(default_factory=JSONThemes)
    max_parallelism: int = 10
    timeout_sec: float = 90.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    shard_specs: list[dict[str, Any]] = Field(default_factory=list)

    @staticmethod
    def _group_files(files: list[str], count: int) -> list[list[str]]:
        groups: list[list[str]] = [[] for _ in range(max(1, count))]
        if not files:
            return groups
        for index, file_path in enumerate(files):
            groups[index % len(groups)].append(file_path)
        return groups

    def build_shards(self) -> list[CodingWorkShard]:
        selected_agents = self.target_agents[: max(1, self.max_parallelism)]
        if not selected_agents:
            return []
        if self.shard_specs:
            shards: list[CodingWorkShard] = []
            for index, spec in enumerate(self.shard_specs[: len(selected_agents)]):
                target_agent = str(spec.get("target_agent") or selected_agents[index]).strip()
                if not target_agent:
                    continue
                shards.append(
                    CodingWorkShard(
                        target_agent=target_agent,
                        target_capability=str(spec.get("target_capability") or "code"),
                        queue_name=f"agent.{target_agent}.tasks",
                        file_targets=list(spec.get("file_targets") or []),
                        objective=str(spec.get("objective") or self.objective),
                        acceptance_criteria=list(spec.get("acceptance_criteria") or self.acceptance_criteria),
                        dependencies=list(spec.get("dependencies") or []),
                        json_themes=JSONThemes(**spec.get("json_themes")) if isinstance(spec.get("json_themes"), dict) else self.json_themes,
                        lane_kind=str(spec.get("lane_kind") or "implement"),
                        worker_role=str(spec.get("worker_role") or "core_logic"),
                        focus_prompt=str(spec.get("focus_prompt") or ""),
                    )
                )
            if shards:
                return shards
        groups = self._group_files(list(self.file_targets), len(selected_agents))
        return [
            CodingWorkShard(
                target_agent=target_agent,
                target_capability="code",
                queue_name=f"agent.{target_agent}.tasks",
                file_targets=groups[index] if index < len(groups) else [],
                objective=self.objective,
                acceptance_criteria=list(self.acceptance_criteria),
                json_themes=self.json_themes,
                worker_role="core_logic",
            )
            for index, target_agent in enumerate(selected_agents)
        ]
