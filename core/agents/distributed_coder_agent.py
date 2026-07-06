from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from core.adapters.state.memory_state_store import MemoryWorkflowStateStore
from core.adapters.state.postgres_state_store import PostgresStateStore
from core.agents.base_agent import BaseAgent
from core.core.distributed_coding_planner import DistributedCodingPlanner
from core.core.distributed_coding_protocol import DistributedCodingTask
from core.core.message_bus import MessageBus
from core.core.models import AgentResult, ResultOutput, Task, TaskEnvelope, TaskPayload, TaskStatus


class DistributedCoderAgent(BaseAgent):
    def __init__(
        self,
        agent_id: str = "distributed-coder-1",
        *,
        message_bus: MessageBus | None = None,
        state_store: PostgresStateStore | MemoryWorkflowStateStore | None = None,
        planner: DistributedCodingPlanner | None = None,
    ) -> None:
        super().__init__(agent_id, ["code", "plan", "test", "review", "async_orchestration", "distributed_coding"])
        self.message_bus = message_bus
        self.state_store = state_store
        self.planner = planner or DistributedCodingPlanner()
        self.set_identity(provider="local", model_name="distributed-coder-core")

    def _resolve_bus(self) -> MessageBus:
        if self.message_bus is not None:
            return self.message_bus
        orchestrator = getattr(self, "orchestrator", None)
        return getattr(orchestrator, "message_bus")

    def _resolve_state_store(self) -> PostgresStateStore | MemoryWorkflowStateStore:
        if self.state_store is not None:
            return self.state_store
        orchestrator = getattr(self, "orchestrator", None)
        return getattr(orchestrator, "state_store")

    def _build_protocol_task(self, task: Task, bus: MessageBus) -> DistributedCodingTask:
        return self.planner.build_task(task, bus, dispatch_agent_id=self.agent_id)

    def _build_envelope(self, task: Task, shard: Any) -> TaskEnvelope:
        objective = str(shard.objective or task.input.description)
        if getattr(shard, 'focus_prompt', ''):
            objective = f"{objective}\n\nLANE FOCUS: {shard.focus_prompt}"
        payload = TaskPayload(
            objective=objective,
            input_data={
                "files": list(shard.file_targets),
                "json_themes": shard.json_themes.as_dict(),
                "workflow_id": task.task_id,
                "lane_kind": getattr(shard, 'lane_kind', 'implement'),
                "focus_prompt": getattr(shard, 'focus_prompt', ''),
            },
            context={
                "project": task.context.project,
                "repo_path": task.context.repo_path,
                "branch": task.context.branch,
                "parent_task_id": task.task_id,
                "dispatch_agent": self.agent_id,
            },
            acceptance_criteria=list(shard.acceptance_criteria),
            expected_output_format="agent_result",
            artifacts=list(shard.file_targets),
        )
        return TaskEnvelope(
            task_id=task.task_id,
            parent_task_id=task.task_id,
            trace_id=task.task_id,
            correlation_id=task.task_id,
            source_agent=self.agent_id,
            target_agent=shard.target_agent,
            target_capability=getattr(shard, "target_capability", "code"),
            priority=task.priority,
            payload=payload,
        )

    def _store_workflow_state(self, store: PostgresStateStore | MemoryWorkflowStateStore, workflow_id: str, state: dict[str, Any]) -> None:
        store.save_workflow(workflow_id, state)

    def _append_event(self, store: PostgresStateStore | MemoryWorkflowStateStore, workflow_id: str, event_type: str, payload: dict[str, Any]) -> None:
        store.append_event(workflow_id, event_type, payload)

    async def run_async(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        bus = self._resolve_bus()
        store = self._resolve_state_store()
        protocol_task = self._build_protocol_task(task, bus)
        shards = protocol_task.build_shards()

        self._store_workflow_state(
            store,
            protocol_task.workflow_id,
            {
                "status": "planning",
                "updated_at": datetime.now(UTC).isoformat(),
                "shard_count": len(shards),
                "target_agents": list(protocol_task.target_agents),
                "planner": protocol_task.metadata.get("planner"),
            },
        )
        self._append_event(
            store,
            protocol_task.workflow_id,
            "distributed_coding.received",
            {
                "objective": protocol_task.objective,
                "repo_path": protocol_task.repo_path,
                "branch": protocol_task.branch,
                "planner": protocol_task.metadata.get("planner"),
            },
        )

        if not protocol_task.target_agents:
            self._store_workflow_state(
                store,
                protocol_task.workflow_id,
                {
                    "status": "failed",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "shard_count": 0,
                    "reason": "no_parallel_peers_available",
                },
            )
            bus.publish(
                "orchestrator.results",
                {
                    "workflow_id": protocol_task.workflow_id,
                    "task_id": task.task_id,
                    "status": "FAILED",
                    "reason": "no_parallel_peers_available",
                },
            )
            return self.result(
                task,
                "Distributed coding dispatch failed: no parallel peers available.",
                TaskStatus.FAILED,
                errors=["no_parallel_peers_available"],
                output=ResultOutput(summary="Distributed coding dispatch failed: no parallel peers available."),
            )

        try:
            await asyncio.wait_for(self._dispatch(protocol_task, task, bus, store, shards), timeout=protocol_task.timeout_sec)
        except asyncio.TimeoutError:
            self._store_workflow_state(
                store,
                protocol_task.workflow_id,
                {
                    "status": "failed",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "shard_count": len(shards),
                    "reason": "dispatch_timeout",
                },
            )
            bus.publish(
                "orchestrator.results",
                {"workflow_id": protocol_task.workflow_id, "status": "FAILED", "reason": "dispatch_timeout"},
            )
            return self.result(
                task,
                "Distributed coding dispatch timed out.",
                TaskStatus.FAILED,
                errors=["dispatch_timeout"],
                output=ResultOutput(summary="Distributed coding dispatch timed out."),
            )

        self._store_workflow_state(
            store,
            protocol_task.workflow_id,
            {
                "status": "dispatched",
                "updated_at": datetime.now(UTC).isoformat(),
                "shard_count": len(shards),
                "target_agents": list(protocol_task.target_agents),
                "planner": protocol_task.metadata.get("planner"),
            },
        )
        self._append_event(
            store,
            protocol_task.workflow_id,
            "distributed_coding.completed",
            {"status": "COMPLETED", "shard_count": len(shards)},
        )
        bus.publish(
            "orchestrator.results",
            {
                "workflow_id": protocol_task.workflow_id,
                "task_id": task.task_id,
                "status": "COMPLETED",
                "shard_count": len(shards),
                "target_agents": list(protocol_task.target_agents),
            },
        )
        return self.result(
            task,
            f"Dispatched {len(shards)} shards for distributed coding.",
            TaskStatus.DONE,
            output=ResultOutput(summary=f"Dispatched {len(shards)} shards for distributed coding."),
        )

    def run(self, task: Task, memory_context: dict | None = None):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(task, memory_context=memory_context))
        return self.result(
            task,
            "Distributed coder requires async execution; call run_async from orchestrator runtime.",
            TaskStatus.FAILED,
            errors=["run_async_required"],
        )

    async def _dispatch(
        self,
        protocol_task: DistributedCodingTask,
        task: Task,
        bus: MessageBus,
        store: PostgresStateStore | MemoryWorkflowStateStore,
        shards: list[Any],
    ) -> None:
        for shard in shards:
            envelope = self._build_envelope(task, shard)
            bus.publish(shard.queue_name, envelope)
            self._append_event(
                store,
                protocol_task.workflow_id,
                "distributed_coding.shard_dispatched",
                {
                    "shard_id": shard.shard_id,
                    "target_agent": shard.target_agent,
                    "queue_name": shard.queue_name,
                    "file_targets": list(shard.file_targets),
                    "lane_kind": getattr(shard, 'lane_kind', 'implement'),
                },
            )
            await asyncio.sleep(0)
