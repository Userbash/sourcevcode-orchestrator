from __future__ import annotations

import asyncio
import importlib
import os
import py_compile
from pathlib import Path

from core.core.orchestrator import Orchestrator


ROOT = Path(__file__).resolve().parents[2]
CORE_RUNTIME_DIR = ROOT / "core" / "core"


def _build_preflight_orchestrator(monkeypatch) -> Orchestrator:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("AI_BRIDGE_DISABLE_SOURCECRAFT", "true")
    monkeypatch.setattr("core.core.orchestrator.MimoOrchestrationDirector.safe_sync", lambda self: None)
    monkeypatch.setattr("core.core.orchestrator.ExperiencePolicyLearner.refresh", lambda self, persistent=None: None)
    monkeypatch.setattr("core.core.orchestrator.ExperienceTrainingPipeline.train", lambda self, persistent=None, runtime_snapshot=None, repo_path=None: None)
    monkeypatch.setattr("core.core.orchestrator.Orchestrator._start_nonblocking_autostarts", lambda self: None)
    monkeypatch.setattr("core.core.local_model_manager_module.LocalModelManagerModule._sync_local_residents_locked", lambda self: None)
    return Orchestrator()


def _core_python_files() -> list[Path]:
    return sorted(
        path
        for path in CORE_RUNTIME_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_all_core_runtime_python_files_compile():
    failures: list[str] = []

    for path in _core_python_files():
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"{path.relative_to(ROOT)}: {exc.msg}")

    assert not failures, "Core runtime compile failures:\n" + "\n".join(failures)


def test_critical_runtime_modules_importable():
    modules = [
        "core.core.orchestrator",
        "core.core.orchestrator_transport",
        "core.core.task_submission_api",
        "core.core.kernel_module_manager",
        "core.core.self_diagnostic_module",
        "core.core.core_healthcheck",
        "core.core.model_selector",
        "core.core.provider_inventory_service",
        "core.core.openai_runtime_router",
        "core.core.local_llm_bridge",
        "core.core.distributed_coding_planner",
    ]

    for module_name in modules:
        importlib.import_module(module_name)


def test_orchestrator_preflight_bootstrap_smoke(monkeypatch):
    orchestrator = _build_preflight_orchestrator(monkeypatch)
    loaded = set(orchestrator.loaded_kernel_modules())
    state = orchestrator.module_state()
    snapshot = orchestrator.monitoring_snapshot()

    assert "ai_activity" in loaded
    assert "orchestrator_control" in loaded
    assert isinstance(state, dict)
    assert "model_availability" in state
    assert "provider_inventory" in state
    assert snapshot["source_of_truth"] == "orchestrator"
    assert isinstance(snapshot["tasks"], dict)


def test_self_diagnostic_preflight_readiness(monkeypatch):
    orchestrator = _build_preflight_orchestrator(monkeypatch)
    module = orchestrator.get_module("self_diagnostic")

    assert module is not None

    report = asyncio.run(
        module.run_diagnostics(
            layers=["components", "memory", "ai_models", "transport"],
            include_layer_matrix=True,
        )
    )

    assert report["status"] in {"healthy", "degraded"}
    assert isinstance(report["readiness"], dict)
    assert "core_ready" in report["readiness"]
    assert "provider_ready" in report["readiness"]
    assert "blocking_issues" in report["readiness"]
    assert "components" in report["requested_layers"]
    assert "memory" in report["requested_layers"]
    assert isinstance(report["layer_checks"], list)
    assert isinstance(report["remediation_plan"], list)
    assert "transport" in report


def test_submit_user_task_preflight_path_returns_structured_result(monkeypatch):
    orchestrator = _build_preflight_orchestrator(monkeypatch)

    monkeypatch.setattr(
        orchestrator,
        "run_sync",
        lambda task: {
            "status": "done",
            "task_id": task.task_id,
            "type": task.type.value,
            "description": task.input.description,
        },
    )

    result = orchestrator.submit_user_task(
        {
            "type": "plan",
            "description": "Preflight orchestrator task",
            "session_id": "preflight-session",
        },
        source="test",
    )

    assert result["status"] == "done"
    assert result["type"] == "plan"
    assert result["description"] == "Preflight orchestrator task"


def test_orchestrator_preflight_has_required_runtime_components(monkeypatch):
    orchestrator = _build_preflight_orchestrator(monkeypatch)

    assert orchestrator.registry is not None
    assert orchestrator.router is not None
    assert orchestrator.scheduler is not None
    assert orchestrator.host_bridge is not None
    assert orchestrator.module_manager is not None
    assert orchestrator.session_memory is not None
    assert orchestrator.delivery_supervisor is not None
