from __future__ import annotations

from fastapi.testclient import TestClient

from core.scripts.orchestrator_daemon import _build_http_app


class _FakeDiagModule:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def run_diagnostics(self, layers=None, matrix_only=False):
        self.calls.append({"layers": layers, "matrix_only": matrix_only})
        return self.payload(matrix_only) if callable(self.payload) else self.payload


class _FakeOrchestrator:
    def __init__(self, diag_module=None):
        self._diag_module = diag_module

    def get_module(self, name):
        if name == "self_diagnostic":
            return self._diag_module
        return None


def test_diagnostics_route_returns_contract_payload_and_passes_query_layers():
    diag = _FakeDiagModule(lambda matrix_only: {
        "schema_version": "diagnostics.v1",
        "status": "healthy",
        "requested_layers": ["memory", "providers"],
        "diagnostic_matrix": {"source": "diagnostic_contracts"},
    })
    client = TestClient(_build_http_app(_FakeOrchestrator(diag)))

    response = client.get("/diagnostics", params=[("layer", "memory"), ("layer", "providers")])

    assert response.status_code == 200
    assert response.json()["schema_version"] == "diagnostics.v1"
    assert diag.calls == [{"layers": ["memory", "providers"], "matrix_only": False}]


def test_diagnostics_route_supports_matrix_only():
    diag = _FakeDiagModule(lambda matrix_only: {
        "schema_version": "diagnostics.v1",
        "layers": ["memory"],
        "matrix": {"schema_version": "diagnostics.v1", "layers": {"memory": {"summary": "memory"}}},
    })
    client = TestClient(_build_http_app(_FakeOrchestrator(diag)))

    response = client.get("/diagnostics", params={"layer": "memory", "matrix_only": "true"})

    assert response.status_code == 200
    assert response.json()["layers"] == ["memory"]
    assert diag.calls == [{"layers": ["memory"], "matrix_only": True}]


def test_diagnostics_route_returns_503_when_module_missing():
    client = TestClient(_build_http_app(_FakeOrchestrator(None)))

    response = client.get("/diagnostics")

    assert response.status_code == 503
    assert response.json()["failure_code"] == "SELF_DIAGNOSTIC_MODULE_UNAVAILABLE"
