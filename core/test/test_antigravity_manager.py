import pytest
from unittest.mock import MagicMock, patch
from core.core.integrations.antigravity_manager import AntigravityManager

def test_antigravity_manager_checks_readiness():
    # Mock HostBridge
    mock_bridge = MagicMock()
    
    manager = AntigravityManager(host_bridge=mock_bridge)
    
    # Mock _run_agy directly on the manager instance
    with patch.object(manager, "_run_agy") as mock_run:
        # Define behavior for models and healthcheck
        def side_effect(args):
            if "models" in args:
                return {"ok": True, "stdout": "model1\nmodel2\n", "stderr": ""}
            # args is ["-p", "healthcheck: ..."]
            if any("healthcheck" in arg for arg in args):
                return {"ok": True, "stdout": "ok", "stderr": ""}
            return {"ok": False}
        mock_run.side_effect = side_effect
        
        # Test readiness check
        assert manager.is_ready() is True
        
        # Test models list
        models = manager.list_models()
        assert "model1" in models
        assert "model2" in models
        
def test_antigravity_manager_handles_not_ready():
    # Mock HostBridge
    mock_bridge = MagicMock()
    
    manager = AntigravityManager(host_bridge=mock_bridge)
    
    with patch.object(manager, "verify_auth", return_value={"ok": False, "stderr": "not ready"}), patch.object(manager, "ensure_authorized", return_value={"ok": False, "stderr": "not ready"}), patch.object(manager, "probe_api_key_models", return_value={"ok": False, "models": [], "error": "missing_api_key", "auth_mode": "api_key"}), patch.object(manager, "_run_agy") as mock_run:
        # Mock 'agy models' failure
        mock_run.return_value = {"ok": False, "stdout": "", "stderr": "Error"}
        
        # Test readiness check
        assert manager.is_ready() is False



def test_antigravity_manager_uses_absolute_login_helper_path(monkeypatch):
    mock_bridge = MagicMock()
    manager = AntigravityManager(host_bridge=mock_bridge)

    with patch.object(manager, "_run_host") as mock_run_host:
        mock_run_host.return_value = {"ok": False, "stderr": "not ready"}
        manager._run_login_helper(["--verify"], timeout=10)
        cmd = mock_run_host.call_args.args[0]
        assert cmd[0] == "python3"
        assert cmd[1].endswith("core/scripts/antigravity_login.py")


def test_antigravity_manager_confirmed_ready_after_login(monkeypatch):
    mock_bridge = MagicMock()
    manager = AntigravityManager(host_bridge=mock_bridge)

    calls = {"verify": 0, "login": 0, "models": 0, "probe": 0}

    def fake_run_login(args, timeout=None):
        if "--verify" in args:
            calls["verify"] += 1
            return {"ok": False, "stderr": "not ready"}
        if "--login" in args:
            calls["login"] += 1
            return {"ok": True, "stdout": "login complete", "stderr": ""}
        return {"ok": False}

    def fake_run_agy(args, timeout=None):
        if args == ["models"]:
            calls["models"] += 1
            if calls["models"] == 1:
                return {"ok": False, "stdout": "", "stderr": "Error"}
            return {"ok": True, "stdout": "model-a\n", "stderr": ""}
        if args and args[0] == "-p":
            calls["probe"] += 1
            return {"ok": True, "stdout": "ok", "stderr": ""}
        return {"ok": False}

    monkeypatch.setattr(manager, "verify_auth", lambda: {"ok": False, "stderr": "not ready"})
    monkeypatch.setattr(manager, "_run_login_helper", fake_run_login)
    monkeypatch.setattr(manager, "_run_agy", fake_run_agy)

    result = manager.ensure_authorized()
    assert result["ok"] is True
    assert result["action"] == "login_confirmed"
    assert calls["login"] == 1
    assert calls["models"] >= 2
    assert calls["probe"] >= 1



def test_antigravity_status_module_snapshot(monkeypatch):
    from core.core.antigravity_status_module import AntigravityStatusModule

    class _Manager:
        def status(self):
            return {
                "ready": True,
                "models": ["model-a"],
                "models_probe": {"ok": True},
                "generation_probe": {"ok": True},
                "auth_probe": {"ok": True},
                "api_probe": {},
                "auth_mode": "agy_oauth",
            }

        def ensure_authorized(self):
            return {"ok": True}

        def verify_auth(self):
            return {"ok": True}

    module = AntigravityStatusModule()
    monkeypatch.setattr(AntigravityStatusModule, "_manager", lambda self: _Manager())
    snap = module.refresh(force=False)
    assert snap["ready"] is True
    assert snap["status"] == "ready"
    assert snap["models"] == ["model-a"]


def test_mimo_director_exposes_antigravity_snapshot():
    from core.mimo.proxy import MimoOrchestrationDirector

    director = MimoOrchestrationDirector()
    director.set_status_source(lambda: {"status": "ready", "ready": True})
    snap = director.antigravity_snapshot()
    assert snap["status"] == "ready"
    assert snap["ready"] is True


def test_antigravity_status_module_finalize_exposes_state(monkeypatch):
    from core.core.antigravity_status_module import AntigravityStatusModule

    class _Manager:
        def status(self):
            return {
                "ready": True,
                "models": ["model-a"],
                "models_probe": {"ok": True},
                "generation_probe": {"ok": True},
                "auth_probe": {"ok": True},
                "api_probe": {},
                "auth_mode": "agy_oauth",
            }

    module = AntigravityStatusModule()
    monkeypatch.setattr(AntigravityStatusModule, "_manager", lambda self: _Manager())
    module.refresh(force=False)
    state = module.finalize()
    assert state["snapshot"]["ready"] is True
    assert state["snapshot"]["models"] == ["model-a"]
