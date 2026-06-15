from __future__ import annotations

import pytest

from core.adapters.state.memory_state_store import MemoryWorkflowStateStore
from core.adapters.state.postgres_state_store import PostgresStateStore
from core.core.cache_guard import CacheGuard, GuardAction
from core.core.model_usage_module import ModelUsageModule
from core.core.models import AgentResult, Task, TaskContext, TaskInput, TaskStatus, TaskType


def test_memory_state_store_supports_versioned_session_state_and_invalidation_audit():
    store = MemoryWorkflowStateStore()

    first = store.save_session_state(
        "sess-1",
        {"context": "alpha"},
        branch="root",
        prompt_version="p1",
        context_version="c1",
    )

    assert first["version"] == 1
    assert first["prompt_version"] == "p1"
    assert first["context_version"] == "c1"

    second = store.save_session_state(
        "sess-1",
        {"context": "beta"},
        branch="root",
        prompt_version="p1",
        context_version="c2",
        expected_version=1,
    )

    assert second["version"] == 2
    assert second["state"]["context"] == "beta"

    store.record_invalidation(
        "sess-1",
        reason="SUMMARY_ROTATION",
        branch="root",
        payload={"dropped_count": 2},
    )
    invalidations = store.recent_invalidations("sess-1", branch="root")
    assert invalidations[-1]["reason"] == "SUMMARY_ROTATION"
    assert invalidations[-1]["payload"]["dropped_count"] == 2


def test_memory_state_store_rejects_stale_expected_version():
    store = MemoryWorkflowStateStore()
    store.save_session_state("sess-1", {"context": "alpha"}, expected_version=None)

    with pytest.raises(ValueError, match="version conflict"):
        store.save_session_state("sess-1", {"context": "beta"}, expected_version=0)


def test_postgres_state_store_maintains_shadow_versioned_state_in_disabled_mode():
    store = PostgresStateStore()

    snapshot = store.save_session_state(
        "sess-2",
        {"context": "shadow"},
        branch="subagent-a",
        prompt_version="p2",
        context_version="c9",
    )

    restored = store.get_session_state("sess-2", branch="subagent-a")
    assert snapshot["version"] == 1
    assert restored["state"]["context"] == "shadow"
    assert restored["prompt_version"] == "p2"
    assert restored["context_version"] == "c9"


def test_cache_guard_escalates_after_three_consecutive_heavy_cache_misses():
    guard = CacheGuard()

    first = guard.observe(
        session_id="sess-guard",
        uncached_input_tokens=60000,
        cached_input_tokens=10000,
        cache_hit_rate=0.14,
    )
    second = guard.observe(
        session_id="sess-guard",
        uncached_input_tokens=62000,
        cached_input_tokens=12000,
        cache_hit_rate=0.16,
    )
    third = guard.observe(
        session_id="sess-guard",
        uncached_input_tokens=64000,
        cached_input_tokens=9000,
        cache_hit_rate=0.12,
    )

    assert first["action"] == GuardAction.WARN.value
    assert second["action"] == GuardAction.SOFT_STOP.value
    assert third["action"] == GuardAction.HARD_STOP.value
    assert third["consecutive_misses"] == 3


def test_model_usage_after_task_records_cache_metrics_and_miss_reason():
    module = ModelUsageModule()
    task = Task(
        TaskType.CODE,
        TaskInput("stabilize prompt cache runtime"),
        TaskContext("demo", ".", "main"),
    )
    context = {
        "model": "mistral-large-latest",
        "provider": "mistral",
        "usage_tokens": 800,
        "usage_cached_input_tokens": 12000,
        "usage_uncached_input_tokens": 58000,
        "usage_output_tokens": 400,
        "cache_hit_rate": 0.17,
        "cache_miss_reason": "PROMPT_CHANGED",
        "prompt_version": "p3",
        "context_version": "c7",
    }
    module.before_task(task, context)
    result = AgentResult(
        task.task_id,
        "mistral-1",
        TaskStatus.DONE,
        {"summary": "ok"},
        0.9,
        [],
        [],
        provider="mistral",
        model_name="mistral-large-latest",
    )

    module.after_task(task, result, context)

    record = module.history[-1]
    assert record["cached_input_tokens"] == 12000
    assert record["uncached_input_tokens"] == 58000
    assert record["cache_hit_rate"] == 0.17
    assert record["cache_miss_reason"] == "PROMPT_CHANGED"
    assert record["prompt_version"] == "p3"
    assert record["context_version"] == "c7"
