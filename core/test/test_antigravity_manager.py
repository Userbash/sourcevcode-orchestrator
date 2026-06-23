from __future__ import annotations

from unittest.mock import MagicMock

from core.core.integrations.antigravity_manager import AntigravityManager


def test_antigravity_manager_status_is_ready_from_api_probes(monkeypatch):
    manager = AntigravityManager(host_bridge=MagicMock())
    monkeypatch.setattr(manager, "probe_api_key_models", lambda: {"ok": True, "models": ["model1", "model2"], "stdout": "model1\nmodel2\n", "stderr": "", "auth_mode": "api_key", "probe_kind": "inventory", "inventory_source": "api_key"})
    monkeypatch.setattr(manager, "_generation_probe", lambda models: {"ok": True, "stdout": "ok", "stderr": "", "status_code": 200, "auth_mode": "api_key"})

    status = manager.status()

    assert status["ready"] is True
    assert status["models"] == ["model1", "model2"]
    assert status["auth_mode"] == "api_key"
    assert status["generation_probe"]["ok"] is True


def test_antigravity_manager_status_uses_registry_fallback(monkeypatch):
    manager = AntigravityManager(host_bridge=MagicMock())
    monkeypatch.setattr(manager, "probe_api_key_models", lambda: {"ok": False, "models": [], "stderr": "missing_api_key", "error": "missing_api_key", "auth_mode": "api_key", "probe_kind": "inventory", "inventory_source": "api_key"})
    monkeypatch.setattr(manager, "_generation_probe", lambda models: {"ok": False, "stdout": "", "stderr": "missing_api_key", "error": "missing_api_key", "status_code": None, "auth_mode": "api_key"})
    monkeypatch.setattr(manager, "_registry_models", lambda force_refresh=False: ["cached-model"])

    status = manager.status()

    assert status["models"] == ["cached-model"]
    assert status["inventory_source"] == "registry"
    assert status["failure_kind"] == "missing_api_key"


def test_antigravity_manager_ensure_authorized_skips_login_and_marks_token_only(monkeypatch):
    manager = AntigravityManager(host_bridge=MagicMock())
    monkeypatch.setattr(manager, "verify_auth", lambda: {"ok": False, "stderr": "missing_api_key", "error": "missing_api_key", "failure_kind": "missing_api_key", "auth_mode": "api_key"})

    result = manager.ensure_authorized()

    assert result["ok"] is False
    assert result["login_suppressed"] is True
    assert result["suppression_reason"] == "api_token_only"
    assert result["auto_login_skipped"] is True


def test_antigravity_manager_session_control_status_reports_api_token_guidance(tmp_path):
    manager = AntigravityManager(host_bridge=MagicMock())
    manager.session_store = manager.session_store.__class__(state_dir=tmp_path / "state", legacy_state_dir=tmp_path / "legacy")
    manager.status = lambda: {"ready": False, "models": [], "auth_mode": "api_key", "models_probe": {"ok": False, "stderr": "missing_api_key"}, "generation_probe": {"ok": False, "stderr": "missing_api_key"}, "auth_probe": {"ok": False, "failure_kind": "missing_api_key"}, "api_probe": {}, "inventory_source": "api_key"}

    summary = manager.session_control_status()

    assert summary["user_action_required"] is True
    assert summary["session_state"] == "auth_required"
    assert "API token" in summary["message_for_user"]
    assert summary["responsibility"]["session_validation"] == "AntigravityManager"


def test_antigravity_manager_submit_interactive_input_requires_active_session(tmp_path):
    manager = AntigravityManager(host_bridge=MagicMock())
    manager.session_store = manager.session_store.__class__(state_dir=tmp_path / "state", legacy_state_dir=tmp_path / "legacy")

    result = manager.submit_interactive_input("ABC123")

    assert result["ok"] is False
    assert result["error"] == "missing_active_session"


def test_antigravity_manager_verify_auth_marks_failure_kind(monkeypatch):
    manager = AntigravityManager(host_bridge=MagicMock())
    monkeypatch.setattr(manager, "probe_api_key_models", lambda: {"ok": False, "stdout": "", "stderr": "forbidden", "error": "forbidden", "auth_mode": "api_key", "probe_kind": "inventory", "inventory_source": "api_key"})

    verify = manager.verify_auth()

    assert verify["ok"] is False
    assert verify["failure_kind"] == "auth_required"
    assert verify["action"] == "verify"
