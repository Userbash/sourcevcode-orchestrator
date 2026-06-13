from unittest.mock import patch

from core.core.model_selector import ModelSelector
from core.core.mimo_bridge import MimoModel
from core.core.models import Task, TaskContext, TaskInput, TaskType


def _task(task_type: TaskType, description: str) -> Task:
    return Task(task_type, TaskInput(description), TaskContext("demo", ".", "main"))


def test_model_selector_syncs_with_mimo():
    selector = ModelSelector()
    assert not hasattr(selector, "mimo_models") or len(selector.mimo_models) == 0

    mock_mimo_models = [
        MimoModel(full_id="github-copilot/claude-haiku-4.5", id="claude-haiku-4.5", provider="github-copilot", status="active", context_window=200000)
    ]

    with patch("core.core.mimo_bridge.MimoBridge.get_models", return_value=mock_mimo_models):
        selector.sync_with_mimo()

    assert hasattr(selector, "mimo_models")
    assert len(selector.mimo_models) == 1
    assert selector.mimo_models[0].full_id == "github-copilot/claude-haiku-4.5"


def test_model_selector_uses_mimo_preferred_model():
    selector = ModelSelector()
    task = _task(TaskType.DOCS, "write a concise guide")
    choice = selector.select(
        task,
        advisory_context={
            "local_llm": {
                "ready": True,
                "should_delegate": True,
                "preferred_model": "qwen-2.5-7b-instruct",
                "budget_pressure": "low",
                "task_family": "docs",
            }
        },
    )
    assert choice.model_name == "qwen-2.5-7b-instruct"
    assert choice.provider == "local"


def test_model_selector_adjusts_context_depth_from_mimo():
    selector = ModelSelector()
    task = _task(TaskType.PLAN, "prepare a layered execution plan")
    choice = selector.select(
        task,
        advisory_context={
            "local_llm": {
                "ready": True,
                "should_delegate": True,
                "preferred_model": "qwen-2.5-7b-instruct",
                "budget_pressure": "high",
                "context_depth": 5,
                "profile_weights": {"quality": 1.4, "budget": 1.1, "vfs": 1.2},
                "task_family": "plan",
            }
        },
    )
    assert choice.model_name == "qwen-2.5-7b-instruct"
    assert choice.params.context_depth >= 5


def test_model_selector_uses_recommended_model_when_preferred_missing():
    selector = ModelSelector()
    task = _task(TaskType.DOCS, "write a concise guide")
    choice = selector.select(
        task,
        advisory_context={
            "local_llm": {
                "ready": True,
                "should_delegate": True,
                "recommended_model": "qwen-2.5-7b-instruct",
                "budget_pressure": "low",
                "task_family": "docs_workflow",
            }
        },
    )
    assert choice.model_name == "qwen-2.5-7b-instruct"
    assert choice.provider == "local"
