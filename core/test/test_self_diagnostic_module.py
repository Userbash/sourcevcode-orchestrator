import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.core.self_diagnostic_module import SelfDiagnosticModule


class _FakeHealth:
    def __init__(self, payload):
        self._payload = payload

    def as_dict(self):
        return dict(self._payload)


class _FakeModule:
    def __init__(self, payload=None):
        self._payload = payload or {"status": "ready"}

    def finalize(self):
        return dict(self._payload)


class _FakeModuleManager:
    def __init__(self, modules):
        self._modules = modules

    def loaded_modules(self):
        return list(self._modules)

    def get_module(self, name):
        return self._modules[name]


def _build_api(module_manager=None, memory=None):
    api = MagicMock()
    api.log = MagicMock()
    api.module_state = MagicMock(return_value={"worker": {"health": "ok"}, "self_diagnostic": {"status": "active"}})
    api.get_context = MagicMock(side_effect=lambda key: module_manager if key == "module_manager" else None)
    api.get_memory = MagicMock(return_value=memory)
    return api


def test_self_diagnostic_module_initialization():
    module = SelfDiagnosticModule()
    assert module.name == "self_diagnostic"


def test_run_diagnostics_structure(monkeypatch):
    monkeypatch.setattr("core.core.self_diagnostic_module.shared_antigravity_snapshot", lambda force=False: {"ready": True})
    contracts_payload = {
        "ok": True,
        "status": "ok",
        "checks": [
            {"name": "boot_contract", "layer": "components", "status": "ok", "ok": True, "source": "diagnostic_contracts"},
        ],
        "diagnostic_matrix": {
            "source": "diagnostic_contracts",
            "layers": {"components": {"status": "healthy", "ok": True, "check_count": 1}},
            "check_count": 1,
        },
    }
    contracts_module = SimpleNamespace(
        DIAGNOSTIC_SCHEMA_VERSION="diagnostics.v1",
        run_diagnostic_matrix=lambda **kwargs: contracts_payload,
        diagnostic_matrix=lambda **kwargs: {"schema_version": "diagnostics.v1", "layers": {"components": {"summary": "components"}}},
    )
    monkeypatch.setattr(SelfDiagnosticModule, "_load_diagnostic_contracts", staticmethod(lambda: contracts_module))

    module = SelfDiagnosticModule()
    memory = SimpleNamespace(backend=object(), _sessions={"s1": object()})
    manager = _FakeModuleManager({"worker": _FakeModule(), "self_diagnostic": _FakeModule({"status": "active"})})
    api = _build_api(module_manager=manager, memory=memory)
    module._availability = MagicMock()
    module._availability.check_all = MagicMock(return_value={"openai": _FakeHealth({"provider": "openai", "status": "healthy"})})

    asyncio.run(module.on_load(api))
    report = asyncio.run(module.run_diagnostics())

    assert report["schema_version"] == "diagnostics.v1"
    assert report["status"] == "healthy"
    assert "worker" in report["components"]
    assert "self_diagnostic" not in report["components"]
    assert report["memory"]["status"] == "ok"
    assert report["layer_check_status"]["ok"] is True
    assert report["diagnostic_matrix"]["source"] == "diagnostic_contracts"
    assert report["layer_checks"][0]["name"] == "boot_contract"


def test_run_diagnostics_can_filter_layers(monkeypatch):
    monkeypatch.setattr("core.core.self_diagnostic_module.shared_antigravity_snapshot", lambda force=False: {"ready": True})
    contracts_payload = {
        "ok": True,
        "status": "ok",
        "checks": [
            {"name": "memory_contract", "layer": "memory", "status": "ok", "ok": True, "source": "diagnostic_contracts"},
            {"name": "provider_contract", "layer": "ai_models", "status": "ok", "ok": True, "source": "diagnostic_contracts"},
        ],
        "diagnostic_matrix": {"source": "diagnostic_contracts", "layers": {"memory": {"status": "healthy", "ok": True, "check_count": 1}}},
    }
    contracts_module = SimpleNamespace(
        DIAGNOSTIC_SCHEMA_VERSION="diagnostics.v1",
        run_diagnostic_matrix=lambda **kwargs: contracts_payload,
    )
    monkeypatch.setattr(SelfDiagnosticModule, "_load_diagnostic_contracts", staticmethod(lambda: contracts_module))

    module = SelfDiagnosticModule()
    api = _build_api(module_manager=_FakeModuleManager({"worker": _FakeModule()}), memory=SimpleNamespace(backend=object(), _sessions={}))
    module._availability = MagicMock()
    module._availability.check_all = MagicMock(return_value={})

    asyncio.run(module.on_load(api))
    report = asyncio.run(module.run_diagnostics(layers=["memory"]))

    assert report["requested_layers"] == ["memory"]
    assert report["components"] == {}
    assert report["ai_models"] == {}
    assert report["memory"]["status"] == "ok"
    assert len(report["layer_checks"]) == 1
    assert report["layer_checks"][0]["layer"] == "memory"


def test_run_layer_diagnostics_passthrough(monkeypatch):
    monkeypatch.setattr("core.core.self_diagnostic_module.shared_antigravity_snapshot", lambda force=False: {"ready": True})
    expected = {
        "ok": False,
        "status": "degraded",
        "checks": [{"name": "provider_contract", "layer": "ai_models", "status": "error", "ok": False, "source": "diagnostic_contracts"}],
        "diagnostic_matrix": {"source": "diagnostic_contracts", "layers": {"ai_models": {"status": "degraded", "ok": False, "check_count": 1}}},
    }
    contracts_module = SimpleNamespace(
        DIAGNOSTIC_SCHEMA_VERSION="diagnostics.v1",
        run_diagnostic_matrix=lambda **kwargs: expected,
    )
    monkeypatch.setattr(SelfDiagnosticModule, "_load_diagnostic_contracts", staticmethod(lambda: contracts_module))

    module = SelfDiagnosticModule()
    module._availability = MagicMock()
    module._availability.check_all = MagicMock(return_value={})
    asyncio.run(module.on_load(_build_api()))
    result = asyncio.run(module.run_layer_diagnostics(["ai_models"]))

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert result["checks"][0]["name"] == "provider_contract"


def test_run_diagnostics_matrix_only(monkeypatch):
    contracts_module = SimpleNamespace(
        DIAGNOSTIC_SCHEMA_VERSION="diagnostics.v1",
        available_layers=lambda: ["boot", "memory", "providers"],
        diagnostic_matrix=lambda **kwargs: {
            "schema_version": "diagnostics.v1",
            "layers": {
                "memory": {"summary": "memory"},
                "providers": {"summary": "providers"},
            },
            "order": ["boot", "memory", "providers"],
        },
    )
    monkeypatch.setattr(SelfDiagnosticModule, "_load_diagnostic_contracts", staticmethod(lambda: contracts_module))

    module = SelfDiagnosticModule()
    asyncio.run(module.on_load(_build_api()))
    report = asyncio.run(module.run_diagnostics(layers=["memory", "providers"], matrix_only=True))

    assert report == {
        "schema_version": "diagnostics.v1",
        "layers": ["memory", "providers"],
        "matrix": {
            "schema_version": "diagnostics.v1",
            "layers": {
                "memory": {"summary": "memory"},
                "providers": {"summary": "providers"},
            },
            "order": ["memory", "providers"],
        },
    }
