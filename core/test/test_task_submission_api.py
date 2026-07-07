from core.core.models import Priority, TaskType
from core.core.task_submission_api import create_standard_task, normalize_user_payload, validate_normalized_payload


def test_normalize_user_payload_plain_text():
    payload = normalize_user_payload("fix bug in login form")
    assert payload["description"] == "fix bug in login form"


def test_create_standard_task_with_aliases_and_string_lists():
    task = create_standard_task(
        {
            "type": "bug",
            "message": "Repair auth flow",
            "priority": "urgent",
            "files": "backend/auth.ts\nbackend/session.ts",
            "acceptance_criteria": "tests pass\nno regressions",
        }
    )

    assert task.type == TaskType.FIX
    assert task.priority == Priority.CRITICAL
    assert task.input.description == "Repair auth flow"
    assert task.input.files == ["backend/auth.ts", "backend/session.ts"]
    assert task.input.acceptance_criteria == ["tests pass", "no regressions"]



def test_create_standard_task_materializes_websocket_routing_hints():
    task = create_standard_task(
        {
            "message": "Summarize current routing policy",
            "source": "websocket",
            "session_id": "ws-123",
            "provider": "openai",
            "model": "gpt-5-mini",
            "cost_tier": "economy",
            "complexity": "high",
            "priority": "normal",
        }
    )

    assert task.session_id == "ws-123"
    assert task.routing_hints["source"] == "websocket"
    assert task.routing_hints["channel"] == "ws"
    assert task.routing_hints["interactive"] is True
    assert task.routing_hints["provider_preference"] == "openai"
    assert task.routing_hints["cost_tier"] == "economy"
    assert task.routing_hints["requested_model"] == "gpt-5-mini"
    assert task.assigned_model == "gpt-5-mini"
    assert task.complexity.value == "high"
    assert task.routing_hints["frame_orchestrator"]["status"] == "validated"
    assert task.routing_hints["frame_xml_package"].startswith("<orchestrator_package")


def test_create_standard_task_preserves_ingress_routing_hints_and_route_mode():
    task = create_standard_task(
        {
            "type": "plan",
            "message": "Inspect websocket ingress routing",
            "route_mode": "orchestrator",
            "external_chat": True,
            "routing_hints": {
                "ingress_path": "/chat/ws",
                "source_adapter": "gemini-cli",
                "control_plane": "chat",
            },
        }
    )

    assert task.type == TaskType.PLAN
    assert task.routing_hints["route_mode"] == "orchestrator"
    assert task.routing_hints["force_orchestrator"] is True
    assert task.routing_hints["external_chat"] is True
    assert task.routing_hints["ingress_path"] == "/chat/ws"
    assert task.routing_hints["source_adapter"] == "gemini-cli"
    assert task.routing_hints["control_plane"] == "chat"


def test_normalize_user_payload_dict_cleans_unicode_and_lists():
    payload = normalize_user_payload({
        "message": "  Fix​ auth flow—now	 ",
        "files": " backend/auth.ts\n\nfrontend/login.ts ",
        "constraints": ["  keep API stable  ", ""],
    })

    assert payload["message"] == "Fix auth flow-now"
    assert payload["files"] == ["backend/auth.ts", "frontend/login.ts"]
    assert payload["constraints"] == ["keep API stable"]


def test_create_standard_task_attaches_normalized_text_profile_and_parallel_hint():
    task = create_standard_task(
        {
            "message": "Implement backend and frontend changes for auth flow with tests and review",
            "files": "backend/auth.ts\nfrontend/login.ts",
            "acceptance_criteria": "tests pass\nreview completed",
            "type": "code",
        }
    )

    profile = task.routing_hints["normalized_text_profile"]
    assert profile["intent_bucket"] == "code"
    assert profile["scope_bucket"] in {"multi_file", "multi_area"}
    assert profile["execution_shape"] == "parallel_candidate"
    assert profile["decision_mode"] == "heuristic"
    assert profile["decision_trust"] in {"trusted", "rough_hint"}
    assert profile["confidence_score"] >= 0.72
    assert any(item.startswith("files:") for item in profile["matched_rules"])
    assert task.routing_hints["parallelize_code"] is True
    roles = task.routing_hints["frame_orchestrator"]["validation"]["worker_roles"]
    assert any(item["role"] == "core_logic" for item in roles)


def test_create_standard_task_frame_package_marks_missing_test_lane_when_not_explicit():
    task = create_standard_task(
        {
            "message": "Implement repository and validation flow for websocket command handling",
            "files": "core/repos/task_repo.py\ncore/security/validator.py",
            "type": "code",
            "source": "websocket",
        }
    )

    gaps = task.routing_hints["frame_orchestrator"]["semantic_gap"]["gap_scanner"]
    assert "missing_explicit_test_lane" in gaps


def test_create_standard_task_rejects_runtime_ineligible_openai_model(tmp_path, monkeypatch):
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        '{"fully_routable_models": ["gpt-5.5"], "validated_models": [{"model": "claude-opus-4-8", "chat_completions": {"ok": false, "error": "Claude pool has no eligible resources"}, "responses": {"ok": false, "error": "Claude pool has no eligible resources"}}, {"model": "gpt-5.5", "chat_completions": {"ok": true}, "responses": {"ok": true}}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true")

    task = create_standard_task({"message": "Fix routing", "provider": "openai", "model": "claude-opus-4-8"})

    assert task.assigned_model is None
    assert task.routing_hints.get("requested_model") is None
    assert task.routing_hints["requested_model_rejected"] == "claude-opus-4-8"


class _FakeSocratiCodeBridge:
    def __init__(self, *, repo_path=None, **kwargs):
        self.repo_path = repo_path
        self.command = ["fake-socraticode"]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def analyze_task(self, *, task, context, description, task_type, routing_hints):
        return {
            "repo_path": self.repo_path or ".",
            "context_coverage": {
                "score": 0.88,
                "coverage_ratio": 0.88,
                "status": "strong",
                "covered_files": list(task.input.files or []),
                "missing_files": [],
                "summary": "Compact indexed context is ready for this task.",
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
                "text": "Task: auth flow\nSearch: indexed hits already cover auth.ts and session.ts",
                "tools_used": ["codebase_search", "codebase_context_search"],
            },
        }


def test_create_standard_task_applies_socraticode_annotation_before_frame_package(monkeypatch):
    import core.core.task_submission_api as task_submission_api

    monkeypatch.setenv("SOCRATICODE_ENABLED", "true")
    monkeypatch.setattr(task_submission_api, "SocratiCodeBridge", _FakeSocratiCodeBridge)

    task = create_standard_task(
        {
            "message": "Repair auth flow with cheaper context path",
            "files": "backend/auth.ts\nbackend/session.ts",
            "type": "code",
        }
    )

    assert task.routing_hints["socraticode"]["status"] == "applied"
    assert task.routing_hints["socraticode_context_coverage"]["score"] == 0.88
    assert task.routing_hints["socraticode_cost_downgrade"]["preferred_provider"] == "local"
    assert task.routing_hints["frame_orchestrator"]["socraticode"]["status"] == "applied"
    assert task.routing_hints["frame_orchestrator"]["socraticode"]["preferred_provider"] == "local"
    assert task.routing_hints["frame_orchestrator"]["socraticode_context_compaction"]["status"] == "active"
    assert task.routing_hints["frame_orchestrator"]["socraticode_context_compaction"]["raw_file_dump_allowed"] is False
    assert '<socraticode status="applied"' in task.routing_hints["frame_xml_package"]
    assert '<socraticode_context_compaction status="active"' in task.routing_hints["frame_xml_package"]


def test_create_standard_task_applies_strong_coverage_cost_and_parallel_downgrade_before_prompt(monkeypatch):
    import core.core.task_submission_api as task_submission_api

    monkeypatch.setenv("SOCRATICODE_ENABLED", "true")
    monkeypatch.setattr(task_submission_api, "SocratiCodeBridge", _FakeSocratiCodeBridge)

    task = create_standard_task(
        {
            "message": "Implement backend and frontend changes for auth flow with tests and review",
            "files": "backend/auth.ts\nfrontend/login.ts",
            "acceptance_criteria": "tests pass\nreview completed",
            "type": "code",
            "source": "websocket",
            "cost_tier": "interactive",
        }
    )

    assert task.routing_hints["original_cost_tier"] == "interactive"
    assert task.routing_hints["cost_tier"] == "economy"
    assert task.routing_hints["socraticode_cost_tier_applied"] is True
    assert task.routing_hints["original_parallel_branches"] == 3
    assert task.routing_hints["parallel_branches"] == 2
    assert task.routing_hints["socraticode_parallel_branches_applied"] is True


def test_validate_normalized_payload_rejects_trigger_only_markers():
    ok, issues = validate_normalized_payload({"message": "PLAN:"})

    assert ok is False
    assert "empty_or_garbage_description" in issues
