from __future__ import annotations

from core.core.models import Task, TaskContext, TaskInput, TaskType
from core.core.socraticode_module import SocratiCodeModule


class _FakeAPI:
    def __init__(self, *, contexts: dict[str, object] | None = None, modules: dict[str, object] | None = None) -> None:
        self._contexts = contexts or {}
        self._modules = modules or {}
        self.messages: list[tuple[str, str]] = []

    def get_context(self, key: str) -> object | None:
        return self._contexts.get(key)

    def get_module(self, name: str) -> object | None:
        return self._modules.get(name)

    def log(self, level: str, message: str) -> None:
        self.messages.append((level, message))


class _Bridge:
    def analyze_task(self, *, task, context, description, task_type, routing_hints):
        assert task_type == "code"
        assert description == "Implement parallel worker cleanup"
        assert routing_hints["parallel_branches"] == 4
        return {
            "context_coverage": {
                "score": 0.75,
                "covered_files": ["core/core/worker.py", "core/test/test_worker.py"],
                "missing_files": ["core/core/router.py"],
                "summary": "Most implementation context is already present.",
            },
            "cost_downgrade": {
                "eligible": True,
                "target_cost_tier": "economy",
                "preferred_provider": "local",
                "reason": "Coverage is high enough for a cheaper route.",
                "confidence": 0.82,
            },
            "parallelism": {
                "recommended_parallel_branches": 2,
                "reason": "Shared files make four branches wasteful.",
                "confidence": 0.91,
            },
        }


class _ExplodingBridge:
    def analyze_task(self, **kwargs):
        raise RuntimeError("bridge unavailable")


def test_socraticode_module_annotates_supported_tasks():
    module = SocratiCodeModule()
    api = _FakeAPI(contexts={"socraticode_bridge": _Bridge()})
    module.on_load(api)

    task = Task(TaskType.CODE, TaskInput("Implement parallel worker cleanup", files=["core/core/worker.py", "core/core/router.py"]), TaskContext("demo", ".", "main"))
    task.routing_hints = {"parallel_branches": 4, "preferred_provider": "openai"}
    context: dict[str, object] = {}

    module.before_task(task, context)

    assert task.routing_hints["preferred_provider"] == "openai"
    assert task.routing_hints["socraticode"]["status"] == "applied"
    assert task.routing_hints["socraticode"]["bridge_source"] == "context:socraticode_bridge"
    assert task.routing_hints["socraticode_context_coverage"]["score"] == 0.75
    assert task.routing_hints["socraticode_cost_downgrade"]["eligible"] is True
    assert task.routing_hints["socraticode_cost_downgrade"]["target_cost_tier"] == "economy"
    assert task.routing_hints["socraticode_parallelism"]["recommended_parallel_branches"] == 2
    assert task.routing_hints["socraticode_parallelism"]["reduce_by"] == 2
    assert context["socraticode"]["parallelism"]["should_reduce"] is True

    final = module.finalize()
    assert final["bridge_available"] is True
    assert final["annotations_total"] == 1
    assert final["failures_total"] == 0


def test_socraticode_module_fails_open_when_bridge_missing():
    module = SocratiCodeModule()
    module.on_load(_FakeAPI())

    task = Task(TaskType.REVIEW, TaskInput("Review auth handler regression"), TaskContext("demo", ".", "main"))
    task.routing_hints = {"model_template_role": "review_primary"}
    context: dict[str, object] = {}

    module.before_task(task, context)

    assert task.routing_hints["model_template_role"] == "review_primary"
    assert task.routing_hints["socraticode"]["status"] == "unavailable"
    assert context["socraticode"]["status"] == "unavailable"
    assert module.finalize()["annotations_total"] == 0


def test_socraticode_module_fails_open_when_bridge_raises():
    module = SocratiCodeModule()
    api = _FakeAPI(modules={"socraticode_bridge": _ExplodingBridge()})
    module.on_load(api)

    task = Task(TaskType.TEST, TaskInput("Validate websocket inventory fallbacks"), TaskContext("demo", ".", "main"))
    context: dict[str, object] = {}

    module.before_task(task, context)

    assert task.routing_hints["socraticode"]["status"] == "error"
    assert "bridge unavailable" in task.routing_hints["socraticode"]["error"]
    assert module.finalize()["failures_total"] == 1
    assert any(level == "warning" and "annotation failed" in message for level, message in api.messages)


def test_socraticode_module_skips_non_target_task_types():
    module = SocratiCodeModule()
    module.on_load(_FakeAPI(contexts={"socraticode_bridge": _Bridge()}))

    task = Task(TaskType.DOCS, TaskInput("Document bridge selection"), TaskContext("demo", ".", "main"))
    context: dict[str, object] = {}

    module.before_task(task, context)

    assert task.routing_hints == {}
    assert context["socraticode"]["status"] == "skipped"
    assert module.finalize()["skipped_total"] == 1
