from pathlib import Path

from core.agents.mimo_agent import MimoAgent
from core.core.mimo_status import build_mimo_runtime_status, classify_mimo_error, mimo_enabled


def test_classify_mimo_error_categories():
    assert classify_mimo_error("Personal Access Tokens are not supported for this endpoint") == "github_pat_not_supported"
    assert classify_mimo_error("Google Generative AI API key is missing") == "gemini_api_key_missing"
    assert classify_mimo_error("Invalid API Key") == "invalid_api_key"
    assert classify_mimo_error("Illegal access") == "illegal_access"
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


def test_mimo_agent_health_degraded_from_runtime_status(monkeypatch):
    monkeypatch.setattr(
        'core.agents.mimo_agent.build_mimo_runtime_status',
        lambda: {
            'status': 'degraded',
            'ready': False,
            'failed_models_sample': [{'model': 'mimo/mimo-auto', 'error': 'Invalid API Key'}],
            'failure_reason': None,
            'live_inventory_error': None,
        },
    )

    health = MimoAgent().health()

    assert health.status.value == 'degraded'
    assert health.last_error == 'Invalid API Key'



def test_build_mimo_runtime_status_marks_auth_only_failures_as_failed(tmp_path):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "mimo_model_ping_report.json").write_text(
        """{
  "provider": "mimo",
  "ok": 0,
  "failed": 2,
  "models": [
    {"model": "xiaomi/mimo-v2.5", "ok": false, "error": "Invalid API Key"},
    {"model": "mimo/mimo-auto", "ok": false, "error": "Invalid API Key"}
  ]
}
""",
        encoding="utf-8",
    )

    snap = build_mimo_runtime_status(report_dir=report_dir)

    assert snap["ready"] is False
    assert snap["status"] == "failed"
    assert snap["auth_categories"]["invalid_api_key"] == 2


def test_build_mimo_runtime_status_respects_disable_env(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_MIMO_ENABLED', 'false')

    snap = build_mimo_runtime_status()

    assert mimo_enabled() is False
    assert snap['status'] == 'disabled'
    assert snap['ready'] is False
    assert snap['failure_reason'] == 'mimo_disabled_by_env'
    assert snap['suppression_policy']['failure_threshold'] >= 1
