from __future__ import annotations

from fastapi.testclient import TestClient

from core.core.models import ExecutionPlan, Priority, Task, TaskContext, TaskInput, TaskType
from core.core.sourcecraft_module import SourceCraftModule
from core.scripts.orchestrator_daemon import _build_http_app


class _FakeModuleManager:
    def get_module(self, name):
        return None


class _FakeSourceCraftOrchestrator:
    def __init__(self) -> None:
        self.module_manager = _FakeModuleManager()
        self.sourcecraft = SourceCraftModule()
        self.sourcecraft._status = "ready"
        self.last_task = None

    def get_module(self, name):
        if name == "sourcecraft":
            return self.sourcecraft
        return self.module_manager.get_module(name)

    def create_execution_plan(self, task):
        self.last_task = task
        brief = task.routing_hints["sourcecraft_parallel_brief"]
        lane_tasks = []
        lane_ids = []
        for lane in brief["lanes"]:
            lane_task = Task(
                TaskType.REVIEW if lane["capability"] == "review" else TaskType.CODE,
                TaskInput(
                    f"[{lane['lane_id']}] {task.input.description}",
                    files=list(lane["file_targets"]),
                    acceptance_criteria=list(lane["acceptance_criteria"]),
                ),
                task.context,
                Priority.NORMAL,
                parent_task_id=task.task_id,
                draft_layer=f"parallel_code_{lane['lane_id']}",
                routing_hints={
                    "preferred_agent_id": lane["agent_hint"],
                    "fanout_label": lane["lane_kind"],
                },
                dependencies=list(lane_ids) if lane["capability"] == "review" else [],
            )
            lane_task.required_capability = lane["capability"]
            lane_tasks.append(lane_task)
            lane_ids.append(lane_task.task_id)
        return ExecutionPlan(
            root_task_id=task.task_id,
            atomic_tasks=lane_tasks,
            draft_layers=[{"name": "parallel_code", "objective": task.input.description, "parallel": True}],
        )


def test_sourcecraft_parallel_delegate_route_returns_parallel_brief_and_plan():
    app = _build_http_app(_FakeSourceCraftOrchestrator())

    payload = {
        "type": "code",
        "description": "Implement SourceCraft websocket progress streaming across backend, dispatcher, and tests",
        "files": [
            "core/scripts/orchestrator_daemon.py",
            "core/core/orchestrator_ws_dispatcher.py",
            "core/test/test_control_ws_protocol.py",
        ],
        "constraints": ["keep health endpoints on http"],
        "acceptance_criteria": ["backend updated", "tests updated"],
        "session_id": "session-parallel",
    }

    with TestClient(app) as client:
        response = client.post("/sourcecraft/parallel_delegate", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["brief"]["should_parallelize"] is True
    assert body["task"]["routing_hints"]["sourcecraft_parallel_delegate"] is True
    assert len(body["atomic_task_summary"]) >= 2
    assert any(row["required_capability"] == "review" for row in body["atomic_task_summary"])
    assert body["plan"]["draft_layers"][0]["name"] == "parallel_code"
