from __future__ import annotations

from typing import Protocol, TypeAlias

from core.core.models import AgentResult, Task

RuntimeContext: TypeAlias = dict[str, object]


class AgentRuntime(Protocol):
    def execute(self, agent_id: str, task: Task, context: RuntimeContext) -> AgentResult: ...
