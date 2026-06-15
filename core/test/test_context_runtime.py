from __future__ import annotations

from core.core.context.context_manager import NativeContextManager
from core.core.context.context_summarizer import summarize
from core.core.models import Task, TaskContext, TaskInput, TaskType


def _task(model_name: str = "local-small") -> Task:
    return Task(
        TaskType.CODE,
        TaskInput("stabilize context runtime"),
        TaskContext("demo", "/repo/demo", "main"),
        assigned_model=model_name,
    )


def test_context_manager_keeps_recent_chunks_when_window_is_exceeded():
    manager = NativeContextManager()
    task = _task("local-small")
    refs = [
        "OLD_A " * 900,
        "OLD_B " * 900,
        "RECENT_C " * 500,
        "RECENT_D " * 500,
    ]

    context = manager.build_context(task, memory_refs=refs)

    assert context["window"] == 8000
    assert context["was_compressed"] is True
    assert context["dropped_count"] == 2
    assert "RECENT_C" in context["context"]
    assert "RECENT_D" in context["context"]
    assert "OLD_A" not in context["context"]
    assert "OLD_B" not in context["context"]
    assert context["summary_version"] == "v1"
    assert context["summary"]


def test_context_manager_does_not_compress_when_context_fits_window():
    manager = NativeContextManager()
    task = _task("gpt-4o")
    refs = ["keep stable", "recent decision", "small constraint list"]

    context = manager.build_context(task, memory_refs=refs)

    assert context["window"] == 128000
    assert context["was_compressed"] is False
    assert context["dropped_count"] == 0
    assert context["summary"] == ""
    assert "keep stable" in context["context"]
    assert "recent decision" in context["context"]


def test_summarizer_returns_versioned_summary_with_source_count():
    summary = summarize(
        [
            "first chunk with important background",
            "second chunk with migration notes",
            "third chunk with stale cache symptoms",
        ],
        max_chars=140,
    )

    assert summary.startswith("[summary:v1 chunks=3]")
    assert "important background" in summary
    assert len(summary) <= 140
