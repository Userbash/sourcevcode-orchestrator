from core.core.models import Priority, TaskType
from core.core.task_submission_api import create_standard_task, normalize_user_payload


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
