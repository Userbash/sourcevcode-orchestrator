from __future__ import annotations

import asyncio

from core.agents.codex_agent import CodexAgent
from core.agents.planner_agent import PlannerAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType
from core.core.orchestrator import Orchestrator


def test_modprobe_style_load_unload():
    orchestrator = Orchestrator()

    assert "ai_activity" in orchestrator.loaded_kernel_modules()
    assert "orchestrator_control" in orchestrator.loaded_kernel_modules()
    
    orchestrator.unload_kernel_module("ai_activity")
    assert "ai_activity" not in orchestrator.loaded_kernel_modules()

    orchestrator.load_kernel_module("ai_activity")
    assert "ai_activity" in orchestrator.loaded_kernel_modules()



def test_ai_activity_in_final_result():
    orchestrator = Orchestrator()
    orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"))
    orchestrator.attach_local_agent("codex-main", CodexAgent("codex-main"))
    orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"))
    orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"))

    task = Task(TaskType.PLAN, TaskInput("Build feature", acceptance_criteria=["tests pass"]), TaskContext("demo", ".", "main"))
    result = asyncio.run(orchestrator.run(task))

    assert "kernel_modules" in result
    assert "ai_activity" in result
    assert "ai_activity" in result["module_state"]
    assert result["ai_activity"]["total_tasks"] >= 1


def test_submit_user_task_uses_orchestrator_as_source_of_truth():
    orchestrator = Orchestrator()
    orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"))
    orchestrator.attach_local_agent("codex-main", CodexAgent("codex-main"))
    orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"))
    orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"))

    orchestrator.submit_user_task({"type": "plan", "description": "Build feature", "acceptance_criteria": ["tests pass"]}, source="test")
    snapshot = orchestrator.monitoring_snapshot()

    assert snapshot["source_of_truth"] == "orchestrator"
    assert snapshot["submitted_total"] >= 1
    assert snapshot["finished_total"] >= 1
    assert isinstance(snapshot["tasks"], dict)


def test_submit_user_task_idempotency_returns_cached_result():
    from core.core.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    payload = {"type": "plan", "description": "Build page logic", "session_id": "idem-1"}
    first = orchestrator.submit_user_task(payload, source="test")
    second = orchestrator.submit_user_task(payload, source="test")
    assert first.get("status") == second.get("status")
    assert isinstance(second, dict)


def test_submit_user_task_websocket_marks_internal_chat_ingress(monkeypatch):
    orchestrator = Orchestrator()
    captured = {}

    def _fake_run_sync(task):
        captured["source"] = task.routing_hints.get("source")
        captured["channel"] = task.routing_hints.get("channel")
        captured["ingress_path"] = task.routing_hints.get("ingress_path")
        captured["text_preparation_mode"] = task.routing_hints.get("text_preparation_mode")
        captured["auto_prepare_text"] = task.routing_hints.get("auto_prepare_text")
        return {"status": "done"}

    monkeypatch.setattr(orchestrator, "run_sync", _fake_run_sync)

    orchestrator.submit_user_task({"message": "hello from external chat", "session_id": "ws-ingress-1"}, source="websocket")

    assert captured["source"] == "websocket"
    assert captured["channel"] == "ws"
    assert captured["ingress_path"] == "websocket_internal_chat"
    assert captured["text_preparation_mode"] == "automatic"
    assert captured["auto_prepare_text"] is True


class _FakeSocratiCodeBridgeForIngress:
    def __init__(self, *, repo_path=None, **kwargs):
        self.repo_path = repo_path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def analyze_task(self, *, task, context, description, task_type, routing_hints):
        return {
            "repo_path": self.repo_path or ".",
            "context_coverage": {
                "score": 0.9,
                "coverage_ratio": 0.9,
                "status": "strong",
                "covered_files": list(task.input.files or []),
                "missing_files": [],
                "summary": "Compact indexed context is ready on websocket ingress.",
                "indexed": True,
            },
            "cost_downgrade": {
                "eligible": True,
                "target_cost_tier": "economy",
                "preferred_provider": "local",
            },
            "parallelism": {
                "recommended_parallel_branches": 2,
            },
            "routing_recommendations": {
                "prefer_low_cost_lanes": True,
                "target_parallel_branches": 2,
                "prefer_provider": "local",
                "shared_index_ready": True,
            },
            "compact_context": {
                "text": "Task: websocket auth flow\nSearch: indexed coverage already exists\nImpact: prompt can avoid raw file dumps",
                "tools_used": ["codebase_search", "codebase_impact"],
            },
        }


def test_submit_user_task_websocket_flows_through_socraticode_frame_and_prompt(monkeypatch):
    import core.core.task_submission_api as task_submission_api

    monkeypatch.setenv("TESTING", "true")
    orchestrator = Orchestrator()
    captured = {}

    monkeypatch.setenv("SOCRATICODE_ENABLED", "true")
    monkeypatch.setattr(task_submission_api, "SocratiCodeBridge", _FakeSocratiCodeBridgeForIngress)

    def _fake_run_sync(task):
        prompt_module = orchestrator.module_manager.get_module("prompt_optimizer")
        if prompt_module is not None:
            prompt_module.before_task(task, {})
        captured["routing_hints"] = dict(task.routing_hints)
        captured["description"] = task.input.description
        return {"status": "done"}

    monkeypatch.setattr(orchestrator, "run_sync", _fake_run_sync)

    orchestrator.submit_user_task({
        "message": "Build websocket auth flow with compact context",
        "session_id": "ws-socraticode-e2e",
        "files": ["backend/auth.ts", "backend/session.ts"],
        "type": "code",
    }, source="websocket")

    hints = captured["routing_hints"]
    assert hints["source"] == "websocket"
    assert hints["ingress_path"] == "websocket_internal_chat"
    assert hints["socraticode"]["status"] == "applied"
    assert hints["frame_orchestrator"]["socraticode_context_compaction"]["status"] == "active"
    assert '<socraticode_context_compaction status="active"' in hints["frame_xml_package"]
    assert "SOCRATICODE CONTEXT SNAPSHOT:" in captured["description"]
    assert "SOCRATICODE CONTEXT COMPACTION:" in captured["description"]
    assert "FRAME ORCHESTRATION PACKAGE:" in captured["description"]
