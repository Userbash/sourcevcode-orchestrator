from __future__ import annotations

import asyncio

from core.agents.base_agent import BaseAgent
from core.core.models import Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.orchestrator import Orchestrator


class _CodeAgent(BaseAgent):
    def __init__(self, agent_id: str = "code-main") -> None:
        super().__init__(agent_id, ["code"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(task, "Implemented requested code changes.")


def _orchestrator() -> Orchestrator:
    orchestrator = Orchestrator()
    orchestrator.attach_local_agent("code-main", _CodeAgent("code-main"))
    return orchestrator


def test_orchestrator_persists_versioned_session_state_for_task_lifecycle():
    orchestrator = _orchestrator()
    task = Task(
        TaskType.CODE,
        TaskInput("stabilize runtime state handling"),
        TaskContext("demo", ".", "main"),
        session_id="sess-versioned-1",
    )

    result = asyncio.run(orchestrator.run_task_async(task))

    assert result.status == TaskStatus.DONE
    snapshot = orchestrator.state_store.get_session_state("sess-versioned-1")
    assert snapshot is not None
    assert snapshot["version"] >= 2
    assert snapshot["state"]["task_id"] == task.task_id
    assert snapshot["state"]["status"] == "done"
    assert snapshot["prompt_version"] == "v1"
    assert snapshot["context_version"].startswith("task:")


def test_orchestrator_hard_stops_session_after_three_heavy_cache_misses():
    orchestrator = _orchestrator()
    session_id = "sess-guard-1"

    for index in range(3):
        task = Task(
            TaskType.CODE,
            TaskInput(f"stabilize cache miss path {index}"),
            TaskContext("demo", ".", "main"),
            session_id=session_id,
            routing_hints={
                "runtime_usage": {
                    "usage_tokens": 900,
                    "usage_cached_input_tokens": 12000,
                    "usage_uncached_input_tokens": 61000,
                    "usage_output_tokens": 400,
                    "cache_hit_rate": 0.15,
                    "cache_miss_reason": "PROMPT_CHANGED",
                    "prompt_version": "p-cache",
                    "context_version": f"c-cache-{index}",
                }
            },
        )
        result = asyncio.run(orchestrator.run_task_async(task))
        assert result.status == TaskStatus.DONE

    snapshot = orchestrator.cache_guard_snapshot(session_id)
    assert snapshot["consecutive_misses"] == 3
    assert snapshot["action"] == "hard_stop"

    blocked_task = Task(
        TaskType.CODE,
        TaskInput("should be blocked by guard"),
        TaskContext("demo", ".", "main"),
        session_id=session_id,
    )
    blocked = asyncio.run(orchestrator.run_task_async(blocked_task))

    assert blocked.status == TaskStatus.FAILED
    assert "cache guard" in blocked.output["summary"].lower()

    invalidations = orchestrator.state_store.recent_invalidations(session_id)
    assert any(item["reason"] == "PROMPT_CHANGED" for item in invalidations)
    assert any(item["reason"] == "CACHE_GUARD_HARD_STOP" for item in invalidations)
