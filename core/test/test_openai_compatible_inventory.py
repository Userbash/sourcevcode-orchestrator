from __future__ import annotations

from core.core.openai_compatible_inventory import build_orchestrator_templates, build_runtime_model_template_manifest


def test_runtime_model_template_manifest_marks_routable_partial_and_blocked_models():
    payload = build_runtime_model_template_manifest(
        ["gpt-5.5", "claude-sonnet-4-6", "gpt-4o-transcribe", "deepseek-v4-pro"],
        validated_rows=[
            {
                "model": "gpt-5.5",
                "chat_completions": {"ok": True, "status_code": 200},
                "responses": {"ok": True, "status_code": 200},
            },
            {
                "model": "claude-sonnet-4-6",
                "chat_completions": {"ok": True, "status_code": 200},
                "responses": {"ok": False, "status_code": 400, "error": "unsupported model"},
            },
            {
                "model": "deepseek-v4-pro",
                "chat_completions": {"ok": True, "status_code": 200},
                "responses": {"ok": False, "status_code": 429, "error": "rate limited"},
            },
        ],
        base_url="https://codex.sale/v1",
        default_model="gpt-5.5",
    )

    rows = {row["model_name"]: row for row in payload["models"]}

    assert rows["gpt-5.5"]["status"] == "routable"
    assert rows["gpt-5.5"]["kernel_eligible"] is True
    assert rows["gpt-5.5"]["default_candidate"] is True
    assert rows["claude-sonnet-4-6"]["status"] == "blocked"
    assert rows["deepseek-v4-pro"]["status"] == "chat_only"
    assert rows["gpt-4o-transcribe"]["status"] == "non_chat_incompatible"
    assert payload["summary"]["routable_count"] == 1
    assert payload["summary"]["blocked_count"] == 1
    assert payload["summary"]["partial_count"] == 1
    assert payload["summary"]["non_chat_count"] == 1


def test_orchestrator_templates_prefer_routable_models_from_validated_probe_rows():
    payload = build_orchestrator_templates(
        ["gpt-5.5", "gpt-5.4", "claude-sonnet-4-6", "deepseek-v4-pro", "qwen3.7-max"],
        base_url="https://codex.sale/v1",
        validated_rows=[
            {
                "model": "gpt-5.5",
                "chat_completions": {"ok": True, "status_code": 200},
                "responses": {"ok": True, "status_code": 200},
            },
            {
                "model": "gpt-5.4",
                "chat_completions": {"ok": True, "status_code": 200},
                "responses": {"ok": True, "status_code": 200},
            },
            {
                "model": "claude-sonnet-4-6",
                "chat_completions": {"ok": False, "status_code": 503, "error": "Claude pool has no eligible resources"},
                "responses": {"ok": False, "status_code": 503, "error": "Claude pool has no eligible resources"},
            },
            {
                "model": "deepseek-v4-pro",
                "chat_completions": {"ok": True, "status_code": 200},
                "responses": {"ok": True, "status_code": 200},
            },
            {
                "model": "qwen3.7-max",
                "chat_completions": {"ok": True, "status_code": 200},
                "responses": {"ok": False, "status_code": 429, "error": "rate limited"},
            },
        ],
    )

    review_rows = payload["roles"]["review_primary"]
    review_models = [row["model_name"] for row in review_rows]
    review_statuses = [row["runtime_status"] for row in review_rows]

    assert payload["defaults"]["review_model"] == "gpt-5.5"
    assert payload["defaults"]["planning_model"] == "gpt-5.5"
    assert "claude-sonnet-4-6" not in review_models
    assert review_models[:3] == ["gpt-5.5", "deepseek-v4-pro", "gpt-5.4"]
    assert review_statuses[:3] == ["routable", "routable", "routable"]
    assert review_rows[3]["model_name"] == "qwen3.7-max"
    assert review_rows[3]["runtime_status"] == "chat_only"
    assert payload["availability_summary"]["routable_count"] == 3
    assert payload["availability_summary"]["partial_count"] == 1
    assert payload["availability_summary"]["blocked_count"] == 1
    assert payload["availability_summary"]["probe_failed_count"] == 0
