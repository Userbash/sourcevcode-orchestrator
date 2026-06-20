from __future__ import annotations

from core.core.availability import ModelAvailability
from core.core.diagnostic_contracts import (
    DIAGNOSTIC_LAYER_ORDER,
    build_diagnostic_contract_matrix,
    list_diagnostic_contracts,
    run_diagnostic_checks,
    run_layer_diagnostic_check,
)
from core.core.orchestrator import Orchestrator
from core.core.session_memory import MemoryScope


_REQUIRED_METADATA_FIELDS = {
    "summary",
    "entry_points",
    "dependencies",
    "inputs",
    "outputs",
    "invariants",
    "failure_signatures",
    "command_examples",
    "test_targets",
    "covered_modules",
}


def _make_orchestrator(monkeypatch, tmp_path) -> Orchestrator:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("AI_BRIDGE_DISABLE_SOURCECRAFT", "true")
    monkeypatch.setenv("AI_BRIDGE_AUTO_BOOTSTRAP", "false")
    monkeypatch.setenv("AI_BRIDGE_AUTOSTART_LOCAL_LLM", "false")
    monkeypatch.setenv("AI_BRIDGE_AUTOSTART_EASY_DIFFUSION", "false")
    monkeypatch.setenv("AI_BRIDGE_POSTGRES_WATCHDOG_ENABLED", "0")
    monkeypatch.setenv("AI_BRIDGE_LIVE_MODEL_PROBE", "false")
    monkeypatch.setenv("AI_BRIDGE_MESSAGE_BUS_BACKEND", "inmemory")
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path / "memory_store"))
    monkeypatch.setenv("AI_BRIDGE_KPI_LOG_FILE", str(tmp_path / "kpi" / "events.jsonl"))
    monkeypatch.setenv("AI_BRIDGE_KPI_REJECTION_SUMMARY_PATH", str(tmp_path / "kpi" / "summary.json"))
    return Orchestrator()


def test_contract_matrix_contains_required_metadata():
    matrix = build_diagnostic_contract_matrix()
    listed = list_diagnostic_contracts()

    assert list(matrix) == list(DIAGNOSTIC_LAYER_ORDER)
    assert [item["layer"] for item in listed] == list(DIAGNOSTIC_LAYER_ORDER)

    for layer, metadata in matrix.items():
        assert set(metadata) == _REQUIRED_METADATA_FIELDS
        assert metadata["summary"]
        assert metadata["entry_points"]
        assert metadata["invariants"]
        assert metadata["covered_modules"]
        assert layer in DIAGNOSTIC_LAYER_ORDER


def test_run_diagnostic_checks_subset_preserves_order(monkeypatch, tmp_path):
    orchestrator = _make_orchestrator(monkeypatch, tmp_path)
    try:
        report = run_diagnostic_checks(orchestrator, layers=["providers", "memory", "providers"])

        assert report["status"] == "ok"
        assert report["layers"] == ["providers", "memory"]
        assert [item["layer"] for item in report["results"]] == ["providers", "memory"]
        assert report["results"][0]["observed"]["preferred_providers"]
        assert report["results"][1]["observed"]["residual_keys"] == []
    finally:
        orchestrator.shutdown()


def test_run_diagnostic_checks_avoids_live_provider_calls(monkeypatch, tmp_path):
    orchestrator = _make_orchestrator(monkeypatch, tmp_path)

    def _fail_live_probe(*args, **kwargs):
        raise AssertionError("live provider probe should not be used by diagnostic contracts")

    monkeypatch.setattr(ModelAvailability, "check_all", _fail_live_probe)
    monkeypatch.setattr(ModelAvailability, "check_provider", _fail_live_probe)

    try:
        report = run_diagnostic_checks(orchestrator, layers=["providers"])

        assert report["status"] == "ok"
        assert report["results"][0]["layer"] == "providers"
        assert report["results"][0]["ok"] is True
    finally:
        orchestrator.shutdown()


def test_run_diagnostic_checks_with_real_orchestrator(monkeypatch, tmp_path):
    orchestrator = _make_orchestrator(monkeypatch, tmp_path)
    try:
        report = run_diagnostic_checks(orchestrator)
        by_layer = {item["layer"]: item for item in report["results"]}

        assert report["status"] == "ok"
        assert report["layers"] == list(DIAGNOSTIC_LAYER_ORDER)
        assert set(by_layer) == set(DIAGNOSTIC_LAYER_ORDER)
        assert all(item["ok"] is True for item in by_layer.values())
        assert "self_diagnostic" in by_layer["boot"]["observed"]["loaded_modules"]
        assert by_layer["planning"]["observed"]["atomic_task_count"] > 0
        assert by_layer["routing"]["observed"]["assigned_agent"] == "orchestrator"
        assert by_layer["execution"]["observed"]["payload_objective"]
        assert by_layer["providers"]["observed"]["preferred_providers"]
        antigravity = by_layer["providers"]["observed"]["providers"]["antigravity"]
        assert "inventory_ok" in antigravity["structural_probe"]["details"]
        assert antigravity["live_ok"] is None
        assert by_layer["memory"]["observed"]["vfs_probe"]["details"]["read_write_ok"] is True
        assert by_layer["memory"]["observed"]["data_plane_probe"]["details"]["configured"] is False
        assert by_layer["observability"]["observed"]["kpi_log_path"].endswith("events.jsonl")
    finally:
        orchestrator.shutdown()


def test_memory_layer_cleans_up_probe_key(monkeypatch, tmp_path):
    orchestrator = _make_orchestrator(monkeypatch, tmp_path)
    try:
        result = run_layer_diagnostic_check("memory", orchestrator)
        residual = orchestrator.get_memory().list_keys(MemoryScope.SESSION, "diagnostic-contracts")

        assert result["ok"] is True
        assert result["observed"]["residual_keys"] == []
        assert residual == []
    finally:
        orchestrator.shutdown()


def test_antigravity_structural_probe_does_not_treat_router_plan_as_inventory(monkeypatch):
    from core.core.diagnostic_contracts import _provider_structural_probe
    from core.core.model_selector import ModelChoice
    from core.core.models import Complexity

    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "antigravity_nonsecret_key_value_1234567890")

    probe = _provider_structural_probe(
        "antigravity",
        {},
        {
            "models": [],
            "inventory_ok": False,
            "inventory_source": "unavailable",
            "inventory_probe_kind": "binary_presence",
        },
        {"models": ["antigravity-flash", "antigravity-pro"]},
        ModelChoice(model_name="antigravity-flash", provider="antigravity", complexity=Complexity.MEDIUM),
    )

    assert probe["details"]["inventory_ok"] is False
    assert probe["details"]["inventory_source"] == "unavailable"
    assert probe["failure_code"] == "PROVIDER_INVENTORY_EMPTY"


def test_run_layer_diagnostic_check_catches_exceptions(monkeypatch):
    from core.core import diagnostic_contracts

    monkeypatch.setitem(diagnostic_contracts._CHECKS, "boot", lambda api: (_ for _ in ()).throw(RuntimeError("boom")))

    result = diagnostic_contracts.run_layer_diagnostic_check("boot", api=None)

    assert result["ok"] is False
    assert result["failures"] == ["boot_check_exception"]
    assert result["observed"]["error_type"] == "RuntimeError"
