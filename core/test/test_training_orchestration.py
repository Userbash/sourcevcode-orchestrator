from __future__ import annotations

import json
import sys

from core.core.training_orchestration import (
    EXPERIENCE_TRAINING_TASK_BOARD_SCHEMA_VERSION,
    build_experience_training_execution_plan,
    build_experience_training_task_board,
    choose_training_supervisor,
)
from core.scripts import generate_training_task_board as training_task_board_script


def _adapter_state() -> dict[str, object]:
    return {
        "total_records": 14,
        "usable_records": 9,
        "min_samples": 5,
        "min_effective_samples": 3.5,
        "min_signal_score": 0.58,
        "task_profiles": {
            "code": {"training_ready": True, "training_stage": "ready"},
            "review": {"training_ready": False, "training_stage": "collecting"},
        },
    }


def test_choose_training_supervisor_prefers_local_llm_when_ready():
    supervisor = choose_training_supervisor(
        runtime_snapshot={"local_llm_ready": True, "ai_kernel_enabled": True, "provider_inventory_ready": True},
        adapter_state=_adapter_state(),
    )

    assert supervisor["primary"]["owner"] == "local_llm"
    assert supervisor["fallback_chain"][0] == "ai_kernel"
    assert supervisor["ready_profiles"] == 1
    assert supervisor["collecting_profiles"] == 1


def test_choose_training_supervisor_falls_back_to_orchestrator():
    supervisor = choose_training_supervisor(
        runtime_snapshot={"local_llm_ready": False, "ai_kernel_enabled": False, "provider_inventory_ready": False},
        adapter_state=_adapter_state(),
    )

    assert supervisor["primary"]["owner"] == "orchestrator"
    assert supervisor["fallback_chain"] == []
    assert supervisor["support_roles"]["validator"] == "orchestrator"


def test_training_task_board_has_expected_parallel_structure():
    board = build_experience_training_task_board(
        adapter_state=_adapter_state(),
        runtime_snapshot={"local_llm_ready": True, "ai_kernel_enabled": True, "provider_inventory_ready": True},
        repo_path="/repo",
        branch="main",
    )

    assert board["schema_version"] == EXPERIENCE_TRAINING_TASK_BOARD_SCHEMA_VERSION
    assert board["context"]["repo_path"] == "/repo"
    assert board["context"]["branch"] == "main"
    assert board["training_supervisor"]["primary"]["owner"] == "local_llm"
    assert board["merge_order"] == ["curator", "labeler", "policy_analyst", "retrieval_indexer", "integrator", "validator"]
    tasks = {item["owner"]: item for item in board["tasks"]}
    assert set(tasks) == {"lead", "curator", "labeler", "policy_analyst", "retrieval_indexer", "integrator", "validator"}
    assert tasks["curator"]["parallelizable"] is True
    assert tasks["labeler"]["parallelizable"] is True
    assert tasks["policy_analyst"]["parallelizable"] is True
    assert tasks["retrieval_indexer"]["parallelizable"] is True
    assert tasks["integrator"]["parallelizable"] is False
    assert tasks["validator"]["parallelizable"] is False


def test_training_execution_plan_encodes_worker_dependencies():
    plan = build_experience_training_execution_plan(
        adapter_state=_adapter_state(),
        runtime_snapshot={"local_llm_ready": True, "ai_kernel_enabled": True, "provider_inventory_ready": True},
        repo_path="/repo",
        branch="main",
    )
    tasks = {task.routing_hints["worker_role"]: task for task in plan.atomic_tasks}

    lead_id = tasks["lead"].task_id
    assert plan.root_task_id == lead_id
    assert tasks["curator"].dependencies == [lead_id]
    assert tasks["labeler"].dependencies == [lead_id]
    assert tasks["policy_analyst"].dependencies == [lead_id]
    assert tasks["retrieval_indexer"].dependencies == [lead_id]
    assert set(tasks["integrator"].dependencies) == {
        tasks["curator"].task_id,
        tasks["labeler"].task_id,
        tasks["policy_analyst"].task_id,
        tasks["retrieval_indexer"].task_id,
    }
    assert tasks["validator"].dependencies == [tasks["integrator"].task_id]
    assert plan.draft_layers[0]["parallel"] is True


def test_generate_training_task_board_script_outputs_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["generate_training_task_board", "--json"])

    rc = training_task_board_script.main()

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema_version"] == EXPERIENCE_TRAINING_TASK_BOARD_SCHEMA_VERSION
    assert "training_supervisor" in payload
