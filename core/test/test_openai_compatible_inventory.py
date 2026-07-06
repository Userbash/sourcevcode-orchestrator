from __future__ import annotations

from core.core.openai_compatible_inventory import build_runtime_model_template_manifest


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
