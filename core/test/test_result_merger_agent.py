from __future__ import annotations

import asyncio

from core.agents.result_merger_agent import ResultMergerAgent
from core.adapters.state.memory_state_store import MemoryWorkflowStateStore
from core.core.message_bus import MessageBus
from core.core.models import Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.result_merger_protocol import ShardExecutionResult


class _InspectableBus(MessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, object]] = []

    def publish(self, topic: str, message: object) -> None:
        self.published.append((topic, message))
        super().publish(topic, message)


def test_result_merger_merges_non_overlapping_shards():
    async def scenario() -> None:
        bus = _InspectableBus()
        store = MemoryWorkflowStateStore()
        agent = ResultMergerAgent(message_bus=bus, state_store=store)
        task = Task(TaskType.CODE, TaskInput("merge shards"), TaskContext("demo", ".", "main"))
        task.routing_hints = {
            "workflow_id": "wf-merge-ok",
            "expected_shards": 2,
            "shard_results": [
                {
                    "workflow_id": "wf-merge-ok",
                    "task_id": "shard-a",
                    "agent_id": "codex-main",
                    "status": "done",
                    "files_changed": ["core/a.py"],
                    "diff_summary": "added a",
                    "diff": "diff --git a/core/a.py b/core/a.py",
                    "errors": [],
                    "next_actions": ["run_unit_tests"],
                },
                {
                    "workflow_id": "wf-merge-ok",
                    "task_id": "shard-b",
                    "agent_id": "deepseek",
                    "status": "done",
                    "files_changed": ["core/b.py"],
                    "diff_summary": "added b",
                    "diff": "diff --git a/core/b.py b/core/b.py",
                    "errors": [],
                    "next_actions": ["run_unit_tests"],
                },
            ],
        }

        result = await agent.run_async(task)

        assert result.status == TaskStatus.DONE
        assert sorted(result.output.files_changed) == ["core/a.py", "core/b.py"]
        published = [payload for topic, payload in bus.published if topic == "orchestrator.results"]
        assert published
        assert published[-1]["conflict_report"]["has_conflicts"] is False
        assert store.get_workflow("wf-merge-ok")["status"] == "completed"

    asyncio.run(scenario())


def test_result_merger_detects_file_overlap_conflict():
    async def scenario() -> None:
        bus = _InspectableBus()
        store = MemoryWorkflowStateStore()
        agent = ResultMergerAgent(message_bus=bus, state_store=store)

        merged = await agent.merge_workflow_results(
            workflow_id="wf-conflict",
            expected_shards=2,
            shard_results=[
                ShardExecutionResult(
                    workflow_id="wf-conflict",
                    task_id="shard-a",
                    agent_id="codex-main",
                    status=TaskStatus.DONE,
                    files_changed=["core/shared.py"],
                    diff_summary="lane a",
                    diff="diff-a",
                ),
                ShardExecutionResult(
                    workflow_id="wf-conflict",
                    task_id="shard-b",
                    agent_id="claude",
                    status=TaskStatus.DONE,
                    files_changed=["core/shared.py"],
                    diff_summary="lane b",
                    diff="diff-b",
                ),
            ],
        )

        assert merged.status == TaskStatus.FAILED
        assert merged.conflict_report.has_conflicts is True
        assert merged.conflict_report.overlapping_files == ["core/shared.py"]
        assert "resolve_file_overlap" in merged.next_actions
        assert store.get_workflow("wf-conflict")["has_conflicts"] is True

    asyncio.run(scenario())


def test_result_merger_waits_for_incomplete_shard_set():
    async def scenario() -> None:
        bus = _InspectableBus()
        store = MemoryWorkflowStateStore()
        agent = ResultMergerAgent(message_bus=bus, state_store=store)

        merged = await agent.merge_workflow_results(
            workflow_id="wf-waiting",
            expected_shards=3,
            shard_results=[
                ShardExecutionResult(
                    workflow_id="wf-waiting",
                    task_id="shard-a",
                    agent_id="codex-main",
                    status=TaskStatus.DONE,
                    files_changed=["core/a.py"],
                    diff_summary="done a",
                    diff="diff-a",
                ),
                ShardExecutionResult(
                    workflow_id="wf-waiting",
                    task_id="shard-b",
                    agent_id="claude",
                    status=TaskStatus.DONE,
                    files_changed=["core/b.py"],
                    diff_summary="done b",
                    diff="diff-b",
                ),
            ],
        )

        assert merged.status == TaskStatus.WAITING_INPUT
        assert merged.received_shards == 2
        assert merged.expected_shards == 3
        assert merged.next_actions == ["await_remaining_shards"]
        assert store.get_workflow("wf-waiting")["status"] == "waiting"

    asyncio.run(scenario())
