from __future__ import annotations

import asyncio

from core.core.inventory_stream_hub import InventoryStreamHub
from core.core.orchestrator_transport import (
    ai_kernel_ensure_payload,
    ai_kernel_gate_payload,
    diagnostics_payload,
    local_llm_connect_payload,
    local_llm_disconnect_payload,
    local_llm_residents_payload,
    local_llm_warm_payload,
    provider_inventory_payload,
    provider_inventory_single_payload,
    provider_inventory_stream,
    provider_model_lookup_payload,
    provider_models_index_payload,
    provider_models_index_stream,
    provider_runtime_inventory_all_payload,
    provider_runtime_inventory_single_payload,
    provider_runtime_inventory_stream,
    runtime_events_stream,
    socraticode_context_compaction_status_payload,
    socraticode_context_compaction_status_stream,
    transport_audit_payload,
)
from core.core.runtime_event_stream_hub import RuntimeEventStreamHub
from core.scripts.orchestrator_daemon import REQUIRED_HTTP_ENDPOINTS, _assert_required_http_routes, _build_http_app
from fastapi.testclient import TestClient


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


async def _first_event(stream):
    async for item in stream:
        return item
    raise AssertionError("stream yielded no events")


def test_diagnostics_route_returns_contract_payload_and_passes_query_layers():
    diag = _FakeDiagModule(lambda matrix_only: {
        "schema_version": "diagnostics.v1",
        "status": "healthy",
        "requested_layers": ["memory", "providers"],
        "diagnostic_matrix": {"source": "diagnostic_contracts"},
    })

    payload, status_code = asyncio.run(diagnostics_payload(_FakeOrchestrator(diag), layers=["memory", "providers"]))

    assert status_code == 200
    assert payload["schema_version"] == "diagnostics.v1"
    assert diag.calls == [{"layers": ["memory", "providers"], "matrix_only": False}]


def test_diagnostics_route_supports_matrix_only():
    diag = _FakeDiagModule(lambda matrix_only: {
        "schema_version": "diagnostics.v1",
        "layers": ["memory"],
        "matrix": {"schema_version": "diagnostics.v1", "layers": {"memory": {"summary": "memory"}}},
    })

    payload, status_code = asyncio.run(diagnostics_payload(_FakeOrchestrator(diag), layers=["memory"], matrix_only=True))

    assert status_code == 200
    assert payload["layers"] == ["memory"]
    assert diag.calls == [{"layers": ["memory"], "matrix_only": True}]


def test_diagnostics_route_returns_503_when_module_missing():
    payload, status_code = asyncio.run(diagnostics_payload(_FakeOrchestrator(None)))

    assert status_code == 503
    assert payload["failure_code"] == "SELF_DIAGNOSTIC_MODULE_UNAVAILABLE"


class _FakeProviderInventory:
    def __init__(self):
        self._index = {"updated_at": 111, "total_models": 3, "provider_counts": {"openai": 1, "local_llm": 1, "ai_kernel": 1}, "by_model": {"gpt-5.5": {"provider": "openai"}, "qwen2.5:32b-instruct-q4_k_m": {"provider": "local_llm", "resident": True}, "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m": {"provider": "ai_kernel", "resident": True}}, "by_provider": {"openai": ["gpt-5.5"], "local_llm": ["qwen2.5:32b-instruct-q4_k_m"], "ai_kernel": ["hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"]}}

    def build_all_provider_endpoint_inventories(self, **kwargs):
        return {"providers": {"openai": {"provider": "openai"}}, "summary": {"provider_count": 1}}

    def build_provider_endpoint_inventory(self, provider, **kwargs):
        return {"provider": provider, "summary": {"total_models": 2}}

    def build_all_provider_runtime_inventories(self, **kwargs):
        return {"providers": {"local_llm": {"provider": "local_llm"}, "openai": {"provider": "openai"}, "ai_kernel": {"provider": "ai_kernel"}}, "summary": {"provider_count": 3}}

    def build_provider_runtime_inventory(self, provider, **kwargs):
        if provider == "ai_kernel":
            return {"provider": provider, "status": "ready", "models": [{"model_name": "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m", "kernel_eligible": True}], "summary": {"total_models": 1, "kernel_usable_models": 1}}
        return {"provider": provider, "status": "ready", "models": [{"model_name": "qwen2.5:32b-instruct-q4_k_m"}], "summary": {"total_models": 1}}

    def model_index_summary(self):
        return dict(self._index)

    def find_model(self, model_name):
        return dict(self._index["by_model"].get(model_name, {})) or None


class _FakeLocalRuntime:
    def __init__(self):
        self.calls = []

    def list_resident_models_sync(self):
        self.calls.append(("residents", None))
        return [type("Resident", (), {"name": "qwen2.5:32b-instruct-q4_k_m", "size_vram": 3221225472})()]

    def warm_model_sync(self, model_name=None, keep_alive=None, timeout_sec=None):
        self.calls.append(("warm", model_name, keep_alive, timeout_sec))
        return type("WarmResult", (), {"as_dict": lambda self: {"model": model_name or "qwen2.5:32b-instruct-q4_k_m", "metrics": {"load_duration_sec": 0.5}}})()


class _FakeLocalLLMModule:
    def __init__(self):
        self.runtime = _FakeLocalRuntime()
        self.model_name = "qwen2.5:32b-instruct-q4_k_m"
        self.hot_reload_calls = []
        self.unload_calls = []

    def hot_reload(self, model_name):
        self.hot_reload_calls.append(model_name)
        self.model_name = model_name
        return True

    def unload_model(self, model_name=None):
        self.unload_calls.append(model_name)
        return True


class _FakeAIKernelBridge:
    def __init__(self):
        self.calls = []

    def gate(self, *, model_name=None, ensure_ready=False):
        self.calls.append({"model_name": model_name, "ensure_ready": ensure_ready})
        return {
            "provider": "ai_kernel",
            "ready": True,
            "model_name": model_name or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
            "model_alias_present": True,
            "models": ["hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"],
            "probe": {"ok": True, "status_code": 200, "models": ["hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"]},
            "attempted_autostart": ensure_ready,
        }


class _FakeUsageModule:
    def finalize(self):
        return {"history": [], "stats": {"models": {}}}


class _FakeBudgetRouter:
    def suppression_snapshot(self):
        return {"openai": {"reason": "quota", "seconds_remaining": 60}}


class _FakeSocratiCodeModule:
    def finalize(self):
        return {
            "status": "ready",
            "bridge_available": True,
            "bridge_source": "task_submission",
            "last_error": None,
            "last_annotation": {
                "status": "applied",
                "context_coverage": {
                    "score": 0.93,
                    "status": "strong",
                    "missing_files": [],
                },
                "cost_downgrade": {
                    "eligible": True,
                    "target_cost_tier": "economy",
                },
                "parallelism": {
                    "recommended_parallel_branches": 2,
                },
            },
        }


class _FakeModuleManager:
    def __init__(self):
        self.local_llm = _FakeLocalLLMModule()
        self.socraticode = _FakeSocratiCodeModule()

    def get_module(self, name):
        if name == "model_usage":
            return _FakeUsageModule()
        if name == "local_llm":
            return self.local_llm
        if name == "socraticode":
            return self.socraticode
        return None


class _FakeProviderOrchestrator(_FakeOrchestrator):
    def __init__(self):
        super().__init__(None)
        self.provider_inventory = _FakeProviderInventory()
        self.provider_budget_router = _FakeBudgetRouter()
        self.module_manager = _FakeModuleManager()
        self.ai_kernel_bridge = _FakeAIKernelBridge()
        self.inventory_stream_hub = InventoryStreamHub()
        self.inventory_stream_hub.publish({"model_index": self.provider_inventory.model_index_summary(), "runtime_inventory": self.provider_inventory.build_all_provider_runtime_inventories()})
        self.runtime_event_stream_hub = RuntimeEventStreamHub()
        self.runtime_event_stream_hub.publish_agent_event("coder-1", {"status": "ready", "source": "boot"})
        self.hot_refresh_calls = []

    def _refresh_hot_provider_inventory_snapshot(self, force_refresh=False):
        self.hot_refresh_calls.append(force_refresh)
        return {"ok": True}

    async def stream_user_task(self, task_payload, source=None):
        yield {
            "type": "final_result",
            "result": {
                "status": "ok",
                "summary": f"handled:{task_payload['message']}",
                "source": source,
                "session_id": task_payload["session_id"],
                "provider": task_payload.get("provider"),
            },
        }


def test_provider_inventory_routes_return_payloads():
    response_all, status_all = provider_inventory_payload(_FakeProviderOrchestrator())
    response_one, status_one = provider_inventory_single_payload(_FakeProviderOrchestrator(), "openai")

    assert status_all == 200
    assert response_all["data"]["summary"]["provider_count"] == 1
    assert status_one == 200
    assert response_one["data"]["provider"] == "openai"


def test_provider_runtime_inventory_routes_return_payloads():
    response_all, status_all = provider_runtime_inventory_all_payload(_FakeProviderOrchestrator())
    response_one, status_one = provider_runtime_inventory_single_payload(_FakeProviderOrchestrator(), "local_llm")

    assert status_all == 200
    assert response_all["data"]["summary"]["provider_count"] == 3
    assert status_one == 200
    assert response_one["data"]["provider"] == "local_llm"


def test_local_llm_control_routes_call_module_runtime():
    orch = _FakeProviderOrchestrator()

    residents, residents_code = local_llm_residents_payload(orch)
    connect, connect_code = local_llm_connect_payload(orch, {"model_name": "qwen2.5:0.5b"})
    warm, warm_code = local_llm_warm_payload(orch, {"model_name": "qwen2.5:0.5b", "keep_alive": 120, "timeout_sec": 2.0})
    disconnect, disconnect_code = local_llm_disconnect_payload(orch, {"model_name": "qwen2.5:0.5b"})

    assert residents_code == 200
    assert residents["data"]["resident_models"][0]["name"] == "qwen2.5:32b-instruct-q4_k_m"
    assert connect_code == 200
    assert connect["data"]["connected"] is True
    assert orch.module_manager.local_llm.hot_reload_calls == ["qwen2.5:0.5b"]
    assert warm_code == 200
    assert warm["data"]["warmed"] is True
    assert orch.module_manager.local_llm.runtime.calls[1] == ("warm", "qwen2.5:0.5b", 120, 2.0)
    assert disconnect_code == 200
    assert disconnect["data"]["disconnected"] is True
    assert orch.module_manager.local_llm.unload_calls == ["qwen2.5:0.5b"]


def test_provider_models_index_route_returns_payload_and_can_force_refresh():
    orch = _FakeProviderOrchestrator()

    response, status_code = provider_models_index_payload(orch, force_refresh=True)

    assert status_code == 200
    assert response["data"]["total_models"] == 3
    assert orch.hot_refresh_calls == [True]


def test_socraticode_context_compaction_status_routes_return_payloads():
    orch = _FakeProviderOrchestrator()

    response, status_code = socraticode_context_compaction_status_payload(orch)
    streamed = asyncio.run(_first_event(socraticode_context_compaction_status_stream(orch)))

    assert status_code == 200
    assert response["data"]["context_compaction"]["status"] == "active"
    assert response["data"]["context_compaction"]["compaction_mode"] == "compact_context_first"
    assert response["data"]["target_cost_tier"] == "economy"
    assert response["data"]["recommended_parallel_branches"] == 2
    assert streamed["data"]["context_compaction"]["status"] == "active"


def test_provider_inventory_websocket_streams_current_snapshot():
    orch = _FakeProviderOrchestrator()

    message = asyncio.run(_first_event(provider_inventory_stream(orch)))

    assert message["status"] == "ok"
    assert message["data"]["providers"]["openai"]["provider"] == "openai"


def test_provider_runtime_inventory_websocket_streams_current_snapshot():
    orch = _FakeProviderOrchestrator()

    message = asyncio.run(_first_event(provider_runtime_inventory_stream(orch)))

    assert message["status"] == "ok"
    assert message["data"]["providers"]["local_llm"]["provider"] == "local_llm"




def test_provider_runtime_inventory_websocket_route_streams_snapshot():
    app = _build_http_app(_FakeProviderOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/ws/providers/runtime_inventory", subprotocols=["chat.v1"]) as websocket:
            message = websocket.receive_json()

    assert message["status"] == "ok"
    assert message["data"]["providers"]["ai_kernel"]["provider"] == "ai_kernel"


def test_socraticode_context_compaction_http_and_ws_routes_return_snapshot():
    app = _build_http_app(_FakeProviderOrchestrator())

    with TestClient(app) as client:
        response = client.get("/socraticode/context_compaction/status")
        with client.websocket_connect("/ws/socraticode/context_compaction/status", subprotocols=["chat.v1"]) as websocket:
            message = websocket.receive_json()

    assert response.status_code == 200
    assert response.json()["data"]["context_compaction"]["status"] == "active"
    assert message["status"] == "ok"
    assert message["data"]["context_compaction"]["compaction_mode"] == "compact_context_first"


def test_provider_models_index_websocket_streams_current_snapshot():
    orch = _FakeProviderOrchestrator()

    message = asyncio.run(_first_event(provider_models_index_stream(orch)))

    assert message["status"] == "ok"
    assert message["data"]["provider_counts"]["local_llm"] == 1


def test_provider_model_lookup_route_returns_single_model_row():
    orch = _FakeProviderOrchestrator()

    response, status_code = provider_model_lookup_payload(orch, "qwen2.5:32b-instruct-q4_k_m")

    assert status_code == 200
    assert response["data"]["provider"] == "local_llm"
    assert response["data"]["resident"] is True


class _FakeTransportAuditOrchestrator(_FakeOrchestrator):
    def build_transport_audit(self):
        return {
            "status": "ok",
            "summary": {
                "fully_ws": False,
                "control_plane_transport": "http",
                "event_stream_transport": "hybrid",
            },
            "ws_endpoints": ["/chat/ws", "/ws/providers/inventory", "/ws/providers/runtime_inventory", "/ws/providers/models/index"],
            "http_endpoints": ["/health", "/providers/inventory"],
            "message_bus": {"backend": "inmemory", "event_driven": True},
            "direct_calls": ["local_llm", "sourcecraft"],
            "migration_plan": [
                {"phase": "phase_1", "title": "Keep HTTP control-plane"},
                {"phase": "phase_2", "title": "Move event streams to WS"},
            ],
        }


def test_transport_audit_route_returns_payload():
    response, status_code = transport_audit_payload(_FakeTransportAuditOrchestrator(None))

    assert status_code == 200
    data = response["data"]
    assert data["summary"]["fully_ws"] is False
    assert "/chat/ws" in data["ws_endpoints"]
    assert data["migration_plan"][0]["phase"] == "phase_1"



def test_runtime_events_websocket_streams_agent_event_snapshot():
    orch = _FakeProviderOrchestrator()

    message = asyncio.run(_first_event(runtime_events_stream(orch)))

    assert message["status"] == "ok"
    assert message["data"]["agents"]["coder-1"]["status"] == "ready"




def test_chat_websocket_route_accepts_compact_frame_and_returns_final_result():
    app = _build_http_app(_FakeProviderOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/chat/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json({
                "c": 1,
                "v": "ws-user",
                "u": "ping ws",
                "m": "session-1",
                "s": "websocket",
                "o": "ai_kernel",
            })
            message = websocket.receive_json()

    assert message["type"] == "final_result"
    assert message["result"]["status"] == "ok"
    assert message["result"]["summary"] == "handled:ping ws"
    assert message["result"]["provider"] == "ai_kernel"


def test_ai_kernel_gate_routes_return_runtime_gate_and_usable_models():
    orch = _FakeProviderOrchestrator()

    gate, gate_code = ai_kernel_gate_payload(orch)
    ensured, ensured_code = ai_kernel_ensure_payload(orch, {"model_name": "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"})

    assert gate_code == 200
    assert gate["data"]["ready"] is True
    assert gate["data"]["orchestrator_usable_models"] == ["hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"]
    assert ensured_code == 200
    assert ensured["data"]["attempted_autostart"] is True
    assert orch.ai_kernel_bridge.calls == [
        {"model_name": None, "ensure_ready": False},
        {"model_name": "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m", "ensure_ready": True},
    ]


def test_http_app_registers_required_inventory_routes():
    app = _build_http_app(_FakeProviderOrchestrator())

    _assert_required_http_routes(app)

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    for expected in REQUIRED_HTTP_ENDPOINTS:
        assert expected in paths
