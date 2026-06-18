from __future__ import annotations

import json
import sys

from core.core.incident_orchestration import (
    INCIDENT_TASK_BOARD_SCHEMA_VERSION,
    build_secret_incident_execution_plan,
    build_secret_incident_task_board,
)
from core.scripts import generate_incident_task_board as incident_script


def test_incident_task_board_has_expected_structure():
    board = build_secret_incident_task_board(repo_path="/repo", branch="main")

    assert board["schema_version"] == INCIDENT_TASK_BOARD_SCHEMA_VERSION
    assert board["context"]["incident_type"] == "secret_leak"
    assert board["context"]["repo_path"] == "/repo"
    assert board["context"]["branch"] == "main"
    assert board["context"]["known_leak_commit"] == "39509cd"
    assert board["context"]["sanitized_rewrite_commit"] == "edeaa70"
    assert board["context"]["sanitized_rewrite_ref"] == "rewrite/39509cd-sanitized"

    tasks = {item["owner"]: item for item in board["tasks"]}
    assert set(tasks) == {
        "security_operator",
        "git_history_surgeon",
        "public_surface_cleaner",
        "runtime_infra_rotator",
        "validator_auditor",
    }
    assert tasks["security_operator"]["parallelizable"] is False
    assert tasks["git_history_surgeon"]["parallelizable"] is True
    assert tasks["public_surface_cleaner"]["parallelizable"] is True
    assert tasks["runtime_infra_rotator"]["parallelizable"] is True
    assert tasks["validator_auditor"]["parallelizable"] is False
    assert "replacements.txt" in tasks["git_history_surgeon"]["outputs"]
    assert "old_secret -> revoked_at" in tasks["security_operator"]["outputs"]


def test_incident_execution_plan_encodes_serial_then_parallel_flow():
    plan = build_secret_incident_execution_plan(repo_path="/repo", branch="main")
    tasks = {task.routing_hints["worker_role"]: task for task in plan.atomic_tasks}

    security_id = tasks["security_operator"].task_id
    assert plan.root_task_id == security_id
    assert tasks["git_history_surgeon"].dependencies == [security_id]
    assert tasks["public_surface_cleaner"].dependencies == [security_id]
    assert tasks["runtime_infra_rotator"].dependencies == [security_id]
    assert set(tasks["validator_auditor"].dependencies) == {
        tasks["git_history_surgeon"].task_id,
        tasks["public_surface_cleaner"].task_id,
        tasks["runtime_infra_rotator"].task_id,
    }
    assert plan.draft_layers[0]["name"] == "secret_incident_response"
    assert plan.draft_layers[0]["parallel"] is True


def test_incident_task_board_script_outputs_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["generate_incident_task_board", "--json"])

    rc = incident_script.main()

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema_version"] == INCIDENT_TASK_BOARD_SCHEMA_VERSION
    assert payload["context"]["known_leak_commit"] == "39509cd"
