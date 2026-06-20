from pathlib import Path

from core.core.mimo_status import build_mimo_runtime_status, classify_mimo_error


def test_classify_mimo_error_categories():
    assert classify_mimo_error("Personal Access Tokens are not supported for this endpoint") == "github_pat_not_supported"
    assert classify_mimo_error("Google Generative AI API key is missing") == "gemini_api_key_missing"
    assert classify_mimo_error("Invalid API Key") == "invalid_api_key"
    assert classify_mimo_error("No access to model: x") == "no_model_access"


def test_build_mimo_runtime_status_reads_report(tmp_path):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "mimo_model_ping_report.json").write_text(
        """{
  "provider": "mimo",
  "ok": 2,
  "failed": 1,
  "models": [
    {"model": "mimo/mimo-auto", "ok": true, "response_sample": "pong"},
    {"model": "openai/gpt-5.4", "ok": true, "response_sample": "pong"},
    {"model": "github-copilot/claude-haiku-4.5", "ok": false, "error": "Personal Access Tokens are not supported for this endpoint"}
  ]
}
""",
        encoding="utf-8",
    )

    snap = build_mimo_runtime_status(report_dir=report_dir)

    assert snap["report_present"] is True
    assert snap["usable_count"] == 2
    assert snap["failed_count"] == 1
    assert snap["provider_breakdown"]["mimo"]["ok"] == 1
    assert snap["provider_breakdown"]["github-copilot"]["failed"] == 1
    assert snap["auth_categories"]["github_pat_not_supported"] == 1


def test_build_mimo_runtime_status_falls_back_to_usable_artifact(tmp_path):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "mimo_model_ping_report.json").write_text(
        """{
  "provider": "mimo",
  "models": [],
  "ok": 0,
  "failed": 1,
  "error": "stale runtime wrapper"
}
""",
        encoding="utf-8",
    )
    (report_dir / "mimo_usable_models.json").write_text(
        """{
  "provider": "mimo",
  "usable_count": 1,
  "total": 1,
  "models": [
    {"model": "openai/gpt-5.4", "ok": true, "response_sample": "ok", "exit_code": 0}
  ]
}
""",
        encoding="utf-8",
    )

    snap = build_mimo_runtime_status(report_dir=report_dir)

    assert snap["ready"] is True
    assert snap["usable_count"] == 1
    assert snap["failed_count"] == 0
    assert snap["usable_models_sample"] == ["openai/gpt-5.4"]
    assert snap["usable_artifact_present"] is True
