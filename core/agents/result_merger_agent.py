from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from core.adapters.state.memory_state_store import MemoryWorkflowStateStore
from core.adapters.state.postgres_state_store import PostgresStateStore
from core.agents.base_agent import BaseAgent
from core.core.message_bus import MessageBus
from core.core.models import ResultOutput, Task, TaskStatus
from core.core.result_merger_protocol import MergeConflictReport, MergedWorkflowResult, ShardExecutionResult


class ResultMergerAgent(BaseAgent):
    def __init__(
        self,
        agent_id: str = "result-merger",
        *,
        message_bus: MessageBus | None = None,
        state_store: PostgresStateStore | MemoryWorkflowStateStore | None = None,
    ) -> None:
        super().__init__(agent_id, ["fan_in", "merge", "result_validation", "distributed_coding"])
        self.message_bus = message_bus
        self.state_store = state_store
        self.set_identity(provider="local", model_name="result-merger-core")

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

    @staticmethod
    def _coerce_shard_results(raw_results: list[Any]) -> list[ShardExecutionResult]:
        coerced: list[ShardExecutionResult] = []
        for item in raw_results:
            if isinstance(item, ShardExecutionResult):
                coerced.append(item)
            elif isinstance(item, dict):
                coerced.append(ShardExecutionResult(**item))
        return coerced

    @staticmethod
    def _conflict_report(shard_results: list[ShardExecutionResult]) -> MergeConflictReport:
        seen = Counter(file_path for shard in shard_results for file_path in shard.files_changed)
        overlapping = sorted(file_path for file_path, count in seen.items() if count > 1)
        if not overlapping:
            return MergeConflictReport(has_conflicts=False, overlapping_files=[], reasons=[])
        return MergeConflictReport(
            has_conflicts=True,
            overlapping_files=overlapping,
            reasons=[f"file_overlap:{file_path}" for file_path in overlapping],
        )

    @staticmethod
    def _merged_diff(shard_results: list[ShardExecutionResult]) -> str:
        chunks = [chunk.strip() for chunk in (shard.diff for shard in shard_results) if chunk and chunk.strip()]
        return "\n\n".join(chunks)

    @staticmethod
    def _merged_summary(shard_results: list[ShardExecutionResult]) -> str:
        parts = [part.strip() for part in (shard.diff_summary for shard in shard_results) if part and part.strip()]
        return " | ".join(parts)

    async def merge_workflow_results(
        self,
        *,
        workflow_id: str,
        shard_results: list[ShardExecutionResult],
        expected_shards: int | None = None,
        task_id: str | None = None,
    ) -> MergedWorkflowResult:
        bus = self._resolve_bus()
        store = self._resolve_state_store()
        expected = max(0, int(expected_shards or len(shard_results)))
        received = len(shard_results)
        final_task_id = task_id or workflow_id

        store.save_workflow(
            workflow_id,
            {
                "status": "merging",
                "updated_at": datetime.now(UTC).isoformat(),
                "expected_shards": expected,
                "received_shards": received,
            },
        )
        store.append_event(
            workflow_id,
            "result_merger.received",
            {"expected_shards": expected, "received_shards": received},
        )

        if expected and received < expected:
            result = MergedWorkflowResult(
                workflow_id=workflow_id,
                task_id=final_task_id,
                agent_id=self.agent_id,
                status=TaskStatus.WAITING_INPUT,
                files_changed=sorted({file_path for shard in shard_results for file_path in shard.files_changed}),
                diff_summary=self._merged_summary(shard_results),
                merged_diff=self._merged_diff(shard_results),
                errors=[],
                next_actions=["await_remaining_shards"],
                conflict_report=MergeConflictReport(has_conflicts=False),
                received_shards=received,
                expected_shards=expected,
            )
            bus.publish("orchestrator.results", result.as_dict())
            store.append_event(workflow_id, "result_merger.waiting", {"missing_shards": expected - received})
            store.save_workflow(
                workflow_id,
                {
                    "status": "waiting",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "expected_shards": expected,
                    "received_shards": received,
                },
            )
            return result

        conflict_report = self._conflict_report(shard_results)
        merged_files = sorted({file_path for shard in shard_results for file_path in shard.files_changed})
        all_errors = [error for shard in shard_results for error in shard.errors]
        next_actions = [action for shard in shard_results for action in shard.next_actions]
        final_status = TaskStatus.FAILED if conflict_report.has_conflicts or any(shard.status == TaskStatus.FAILED for shard in shard_results) else TaskStatus.DONE
        if conflict_report.has_conflicts:
            next_actions.append("resolve_file_overlap")
        result = MergedWorkflowResult(
            workflow_id=workflow_id,
            task_id=final_task_id,
            agent_id=self.agent_id,
            status=final_status,
            files_changed=merged_files,
            diff_summary=self._merged_summary(shard_results),
            merged_diff=self._merged_diff(shard_results),
            errors=all_errors + conflict_report.reasons,
            next_actions=sorted(set(next_actions)),
            conflict_report=conflict_report,
            received_shards=received,
            expected_shards=expected or received,
        )
        bus.publish("orchestrator.results", result.as_dict())
        store.append_event(
            workflow_id,
            "result_merger.completed",
            {
                "status": result.status.value,
                "received_shards": received,
                "expected_shards": result.expected_shards,
                "has_conflicts": conflict_report.has_conflicts,
            },
        )
        store.save_workflow(
            workflow_id,
            {
                "status": "failed" if result.status == TaskStatus.FAILED else "completed",
                "updated_at": datetime.now(UTC).isoformat(),
                "expected_shards": result.expected_shards,
                "received_shards": received,
                "has_conflicts": conflict_report.has_conflicts,
            },
        )
        return result

    async def run_async(self, task: Task, memory_context: dict | None = None):
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        workflow_id = str(hints.get("workflow_id") or task.task_id)
        expected_shards = hints.get("expected_shards")
        raw_results = hints.get("shard_results") or []
        shard_results = self._coerce_shard_results(raw_results if isinstance(raw_results, list) else [])
        merged = await self.merge_workflow_results(
            workflow_id=workflow_id,
            shard_results=shard_results,
            expected_shards=int(expected_shards) if expected_shards is not None else None,
            task_id=task.task_id,
        )
        return self.result(
            task,
            merged.diff_summary or f"Merged {merged.received_shards} shard results.",
            status=merged.status,
            errors=list(merged.errors),
            output=ResultOutput(
                summary=merged.diff_summary or f"Merged {merged.received_shards} shard results.",
                files_changed=list(merged.files_changed),
                commands_run=[],
                test_results=[],
                diff=merged.merged_diff,
            ),
        )

    def run(self, task: Task, memory_context: dict | None = None):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(task, memory_context=memory_context))
        return self.result(
            task,
            "Result merger requires async execution; call run_async from orchestrator runtime.",
            TaskStatus.FAILED,
            errors=["run_async_required"],
        )
