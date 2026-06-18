from __future__ import annotations

import json
import sys

from core.core.diagnostic_orchestration import (
    ANTIGRAVITY_CLI_BRIDGE_TASK_BOARD_SCHEMA_VERSION,
    DIAGNOSTIC_TASK_BOARD_SCHEMA_VERSION,
    build_antigravity_cli_bridge_execution_plan,
    build_antigravity_cli_bridge_task_board,
    build_diagnostic_expansion_execution_plan,
    build_diagnostic_expansion_task_board,
)
from core.scripts import generate_diagnostic_task_board as task_board_script


def test_diagnostic_task_board_has_expected_parallel_structure():
    board = build_diagnostic_expansion_task_board(repo_path="/repo", branch="main")

    assert board["schema_version"] == DIAGNOSTIC_TASK_BOARD_SCHEMA_VERSION
    assert board["context"]["diagnostic_schema_version"] == "diagnostics.v1"
    assert board["context"]["repo_path"] == "/repo"
    assert board["context"]["branch"] == "main"
    assert board["merge_order"] == ["vfs", "providers", "http", "tests", "integrator", "validator"]

    tasks = {item["owner"]: item for item in board["tasks"]}
    assert set(tasks) == {"lead", "http", "vfs", "providers", "tests", "integrator", "validator"}
    assert tasks["http"]["parallelizable"] is True
    assert tasks["vfs"]["parallelizable"] is True
    assert tasks["providers"]["parallelizable"] is True
    assert tasks["tests"]["parallelizable"] is True
    assert tasks["integrator"]["parallelizable"] is False
    assert tasks["validator"]["parallelizable"] is False
    assert tasks["lead"]["prompt"]
    assert any(item["failure_code"] == "VFS_READ_WRITE_FAILED" for item in board["failure_code_catalog"])
    assert any(item["failure_code"] == "PROVIDER_AUTH_FAILED" for item in board["failure_code_catalog"])


def test_execution_plan_encodes_worker_dependencies():
    plan = build_diagnostic_expansion_execution_plan(repo_path="/repo", branch="main")
    tasks = {task.routing_hints["worker_role"]: task for task in plan.atomic_tasks}

    freeze_id = tasks["lead"].task_id
    assert plan.root_task_id == freeze_id
    assert tasks["http"].dependencies == [freeze_id]
    assert tasks["vfs"].dependencies == [freeze_id]
    assert tasks["providers"].dependencies == [freeze_id]
    assert tasks["tests"].dependencies == [freeze_id]
    assert set(tasks["integrator"].dependencies) == {
        tasks["http"].task_id,
        tasks["vfs"].task_id,
        tasks["providers"].task_id,
        tasks["tests"].task_id,
    }
    assert tasks["validator"].dependencies == [tasks["integrator"].task_id]
    assert plan.draft_layers[0]["parallel"] is True


def test_antigravity_task_board_has_expected_parallel_structure():
    board = build_antigravity_cli_bridge_task_board(repo_path="/repo", branch="main")

    assert board["schema_version"] == ANTIGRAVITY_CLI_BRIDGE_TASK_BOARD_SCHEMA_VERSION
    assert board["context"]["failure_domain"] == "antigravity_cli_bridge"
    assert board["context"]["repo_path"] == "/repo"
    assert board["context"]["branch"] == "main"
    assert board["context"]["observed_manager_status"]["api_probe_status_code"] == 403
    assert board["merge_order"] == ["cli_bridge", "manager", "tests", "integrator", "validator"]

    tasks = {item["owner"]: item for item in board["tasks"]}
    assert set(tasks) == {"lead", "cli_bridge", "manager", "tests", "integrator", "validator"}
    assert tasks["cli_bridge"]["parallelizable"] is True
    assert tasks["manager"]["parallelizable"] is True
    assert tasks["tests"]["parallelizable"] is True
    assert tasks["integrator"]["parallelizable"] is False
    assert tasks["validator"]["parallelizable"] is False
    assert tasks["lead"]["prompt"]
    assert any(item["failure_code"] == "ANTIGRAVITY_MODEL_CAPACITY_EXHAUSTED" for item in board["failure_code_catalog"])
    assert any(item["failure_code"] == "ANTIGRAVITY_TIMEOUT_MASKED_UPSTREAM_ERROR" for item in board["failure_code_catalog"])


def test_antigravity_execution_plan_encodes_worker_dependencies():
    plan = build_antigravity_cli_bridge_execution_plan(repo_path="/repo", branch="main")
    tasks = {task.routing_hints["worker_role"]: task for task in plan.atomic_tasks}

    freeze_id = tasks["lead"].task_id
    assert plan.root_task_id == freeze_id
    assert tasks["cli_bridge"].dependencies == [freeze_id]
    assert tasks["manager"].dependencies == [freeze_id]
    assert tasks["tests"].dependencies == [freeze_id]
    assert set(tasks["integrator"].dependencies) == {
        tasks["cli_bridge"].task_id,
        tasks["manager"].task_id,
        tasks["tests"].task_id,
    }
    assert tasks["validator"].dependencies == [tasks["integrator"].task_id]
    assert plan.draft_layers[0]["name"] == "antigravity_cli_bridge_wave"
    assert plan.draft_layers[0]["parallel"] is True


def test_generate_task_board_script_supports_antigravity_wave(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["generate_diagnostic_task_board", "--wave", "antigravity-cli-bridge", "--json"])

    rc = task_board_script.main()

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema_version"] == ANTIGRAVITY_CLI_BRIDGE_TASK_BOARD_SCHEMA_VERSION
    assert payload["context"]["failure_domain"] == "antigravity_cli_bridge"
