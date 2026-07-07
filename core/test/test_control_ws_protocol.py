from __future__ import annotations

from fastapi.testclient import TestClient

import core.core.antigravity_status_module as antigravity_status_module
import core.core.openai_bazzite_endpoint as openai_bazzite_endpoint

from core.core.sourcecraft_module import SourceCraftModule
from core.scripts.orchestrator_daemon import _build_http_app


class _FakeProviderInventory:
    def build_provider_runtime_inventory(self, provider: str, **kwargs):
        if provider == "ai_kernel":
            return {
                "provider": "ai_kernel",
                "status": "ready",
                "models": [
                    {
                        "model_name": "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
                        "kernel_eligible": True,
                    }
                ],
                "summary": {"total_models": 1, "kernel_usable_models": 1},
            }
        if provider == "openai":
            return {
                "provider": "openai",
                "status": "ready",
                "models": [{"model_name": "gpt-5.5"}],
                "summary": {"total_models": 1},
            }
        return {
            "provider": "local_llm",
            "status": "ready",
            "models": [{"model_name": "qwen2.5:0.5b"}],
            "summary": {"total_models": 1},
        }

    def model_index_summary(self):
        return {
            "updated_at": 111,
            "total_models": 2,
            "provider_counts": {"openai": 1, "local_llm": 1},
            "by_model": {"gpt-5.5": {"provider": "openai"}},
            "by_provider": {"openai": ["gpt-5.5"]},
        }

    def refresh_openai_runtime_inventory(self, force_refresh=False, probe_limit=None):
        return {
            "provider": "openai",
            "status": "ready",
            "probe_limit": probe_limit,
            "force_refresh": force_refresh,
            "models": [{"model_name": "gpt-5.5"}],
            "model_templates": {"chat": ["gpt-5.5"]},
        }


class _FakeBudgetRouter:
    def suppression_snapshot(self):
        return {}


class _FakeUsageModule:
    def finalize(self):
        return {"requests": 7}


class _FakeLocalModelManager:
    def finalize(self):
        return {
            "resident_models": [{"name": "qwen2.5:0.5b"}],
            "blocked_models": [],
            "memory_pressure": {"pressure_state": "normal"},
            "evictions": 0,
            "warmups": 3,
        }


class _FakeLocalRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int | None, float | None]] = []

    def warm_model_sync(self, model_name=None, keep_alive=None, timeout_sec=None):
        self.calls.append(("warm", model_name, keep_alive, timeout_sec))
        return type(
            "WarmResult",
            (),
            {"as_dict": lambda self: {"model": model_name, "keep_alive": keep_alive, "timeout_sec": timeout_sec}},
        )()


class _FakeLocalLLMModule:
    def __init__(self) -> None:
        self.runtime = _FakeLocalRuntime()
        self.model_name = "qwen2.5:32b-instruct-q4_k_m"
        self.hot_reload_calls: list[str] = []
        self.unload_calls: list[str | None] = []

    def hot_reload(self, model_name):
        self.hot_reload_calls.append(model_name)
        self.model_name = model_name
        return True

    def unload_model(self, model_name=None):
        self.unload_calls.append(model_name)
        return True


class _FakeDiagModule:
    def run_diagnostics(self, layers=None, matrix_only=False):
        async def _run():
            return {
                "schema_version": "diagnostics.v1",
                "layers": list(layers or ["providers"]),
                "matrix": {"providers": {"summary": "ok"}},
                "matrix_only": matrix_only,
            }

        return _run()


class _FakeModuleManager:
    def __init__(self) -> None:
        self.local_llm = _FakeLocalLLMModule()
        self.sourcecraft = SourceCraftModule()
        self.sourcecraft._status = "ready"

    def loaded_modules(self):
        return ["local_llm", "sourcecraft"]

    def get_module(self, name):
        if name == "model_usage":
            return _FakeUsageModule()
        if name == "local_llm":
            return self.local_llm
        if name == "local_model_manager":
            return _FakeLocalModelManager()
        if name == "sourcecraft":
            return self.sourcecraft
        if name == "self_diagnostic":
            return _FakeDiagModule()
        return None


class _FakeAIKernelBridge:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def gate(self, *, model_name=None, ensure_ready=False):
        self.calls.append({"model_name": model_name, "ensure_ready": ensure_ready})
        return {
            "provider": "ai_kernel",
            "ready": True,
            "model_name": model_name or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
            "attempted_autostart": ensure_ready,
            "models": ["hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"],
            "probe": {"ok": True, "status_code": 200},
        }


class _FakeInventoryHub:
    def __init__(self) -> None:
        self.events = [
            {
                "kind": "snapshot",
                "published_at": 10,
                "version": 1,
                "snapshot": {
                    "providers": {
                        "openai": {
                            "provider": "openai",
                            "status": "ready",
                            "models": [{"model_name": "gpt-5.5"}],
                        }
                    },
                    "summary": {"provider_count": 1},
                },
            }
        ]

    async def stream(self, topic=None):
        for event in self.events:
            yield event


class _FakeRouteAcceptance:
    def as_dict(self):
        return {"assigned_agent": "orchestrator", "message": "fanout recommended"}


class _FakeRouter:
    def route(self, task):
        return _FakeRouteAcceptance()


class _FakePlan:
    def __init__(self, task):
        lane = task.routing_hints["sourcecraft_parallel_brief"]["lanes"][0]
        atomic = type(
            "AtomicTask",
            (),
            {
                "task_id": "atomic-1",
                "type": type("TaskType", (), {"value": "code"})(),
                "draft_layer": "parallel_code_lane_1",
                "required_capability": lane["capability"],
                "routing_hints": {"preferred_agent_id": lane["agent_hint"], "fanout_label": lane["lane_kind"]},
                "dependencies": [],
                "input": type("TaskInput", (), {"files": lane["file_targets"]})(),
            },
        )()
        self.atomic_tasks = [atomic]

    def as_dict(self):
        return {"draft_layers": [{"name": "parallel_code"}]}


class _FakeControlPlaneOrchestrator:
    def __init__(self) -> None:
        self.provider_inventory = _FakeProviderInventory()
        self.provider_budget_router = _FakeBudgetRouter()
        self.module_manager = _FakeModuleManager()
        self.ai_kernel_bridge = _FakeAIKernelBridge()
        self.inventory_stream_hub = _FakeInventoryHub()
        self.router = _FakeRouter()

    def get_module(self, name):
        return self.module_manager.get_module(name)

    def create_execution_plan(self, task):
        return _FakePlan(task)

    def module_state(self):
        return {"provider_inventory": {"providers": ["openai", "local_llm"]}}


def _frame(action: str, data: dict, *, request_id: str = "req-1", correlation_id: str = "corr-1", frame_type: str = "command") -> dict:
    return {
        "type": frame_type,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "action": action,
        "data": data,
        "idempotency_key": f"idem-{request_id}",
        "timeout_ms": 5_000,
    }


def _assert_ack_frame(message: dict, *, action: str, request_id: str = "req-1", correlation_id: str = "corr-1", mode: str = "single") -> None:
    assert message == {
        "type": "ack",
        "request_id": request_id,
        "correlation_id": correlation_id,
        "action": action,
        "data": {
            "accepted": True,
            "mode": mode,
            "idempotency_key": f"idem-{request_id}",
            "timeout_ms": 5_000,
        },
        "error": None,
        "final": False,
        "ack": True,
    }


def test_control_ws_local_llm_commands_emit_ack_then_terminal_response():
    orch = _FakeControlPlaneOrchestrator()
    app = _build_http_app(orch)

    cases = [
        (
            "local_llm/connect",
            {"model_name": "qwen2.5:0.5b"},
            lambda payload: payload["data"]["status"] == "ok" and payload["data"]["data"]["connected"] is True,
        ),
        (
            "local_llm/disconnect",
            {"model_name": "qwen2.5:0.5b"},
            lambda payload: payload["data"]["status"] == "ok" and payload["data"]["data"]["disconnected"] is True,
        ),
        (
            "local_llm/warm",
            {"model_name": "qwen2.5:0.5b", "keep_alive": 120, "timeout_sec": 2.0},
            lambda payload: payload["data"]["data"]["result"]["model"] == "qwen2.5:0.5b",
        ),
    ]

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            for index, (action, data, assertion) in enumerate(cases, start=1):
                request_id = f"req-{index}"
                correlation_id = f"corr-{index}"
                websocket.send_json(_frame(action, data, request_id=request_id, correlation_id=correlation_id))

                ack = websocket.receive_json()
                terminal = websocket.receive_json()

                _assert_ack_frame(ack, action=action, request_id=request_id, correlation_id=correlation_id)
                assert terminal["type"] == "response"
                assert terminal["request_id"] == request_id
                assert terminal["correlation_id"] == correlation_id
                assert terminal["action"] == action
                assert terminal["error"] is None
                assert terminal["final"] is True
                assert assertion(terminal)

    assert orch.module_manager.local_llm.hot_reload_calls == ["qwen2.5:0.5b"]
    assert orch.module_manager.local_llm.unload_calls == ["qwen2.5:0.5b"]
    assert orch.module_manager.local_llm.runtime.calls == [("warm", "qwen2.5:0.5b", 120, 2.0)]


def test_control_ws_ai_kernel_ensure_dispatches_and_returns_terminal_payload():
    orch = _FakeControlPlaneOrchestrator()
    app = _build_http_app(orch)

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json(
                _frame(
                    "ai_kernel/ensure",
                    {"model_name": "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"},
                    request_id="req-ai-kernel",
                    correlation_id="corr-ai-kernel",
                )
            )

            ack = websocket.receive_json()
            terminal = websocket.receive_json()

    _assert_ack_frame(
        ack,
        action="ai_kernel/ensure",
        request_id="req-ai-kernel",
        correlation_id="corr-ai-kernel",
    )
    assert terminal["type"] == "response"
    assert terminal["request_id"] == "req-ai-kernel"
    assert terminal["correlation_id"] == "corr-ai-kernel"
    assert terminal["action"] == "ai_kernel/ensure"
    assert terminal["error"] is None
    assert terminal["final"] is True
    assert terminal["data"]["data"]["ready"] is True
    assert terminal["data"]["data"]["attempted_autostart"] is True
    assert terminal["data"]["data"]["orchestrator_usable_models"] == ["hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"]
    assert orch.ai_kernel_bridge.calls == [
        {
            "model_name": "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
            "ensure_ready": True,
        }
    ]


def test_control_ws_sourcecraft_parallel_delegate_streams_brief_and_plan_events():
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json(
                _frame(
                    "sourcecraft.parallel_delegate",
                    {
                        "type": "code",
                        "description": "Implement websocket migration for SourceCraft and diagnostics",
                        "files": [
                            "core/scripts/orchestrator_daemon.py",
                            "core/core/orchestrator_ws_dispatcher.py",
                        ],
                    },
                    request_id="req-sourcecraft",
                    correlation_id="corr-sourcecraft",
                )
            )

            ack = websocket.receive_json()
            accepted = websocket.receive_json()
            brief_ready = websocket.receive_json()
            plan_ready = websocket.receive_json()

    _assert_ack_frame(ack, action="sourcecraft.parallel_delegate", request_id="req-sourcecraft", correlation_id="corr-sourcecraft", mode="stream")
    assert accepted["type"] == "event"
    assert accepted["data"]["stage"] == "accepted"
    assert brief_ready["type"] == "event"
    assert brief_ready["data"]["stage"] == "brief_ready"
    assert brief_ready["data"]["brief"]["should_parallelize"] is True
    assert plan_ready["type"] == "event"
    assert plan_ready["data"]["stage"] == "plan_ready"
    assert plan_ready["final"] is True
    assert plan_ready["data"]["plan"]["draft_layers"][0]["name"] == "parallel_code"


def test_control_ws_diagnostics_subscription_streams_progress_and_final_payload():
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json(
                _frame(
                    "diagnostics.subscribe",
                    {"layers": ["providers"], "matrix_only": True},
                    request_id="req-diagnostics",
                    correlation_id="corr-diagnostics",
                    frame_type="subscribe",
                )
            )

            ack = websocket.receive_json()
            started = websocket.receive_json()
            completed = websocket.receive_json()

    _assert_ack_frame(ack, action="diagnostics.subscribe", request_id="req-diagnostics", correlation_id="corr-diagnostics", mode="stream")
    assert started["type"] == "event"
    assert started["data"]["stage"] == "started"
    assert completed["type"] == "event"
    assert completed["data"]["stage"] == "completed"
    assert completed["final"] is True
    assert completed["data"]["payload"]["schema_version"] == "diagnostics.v1"


def test_control_ws_provider_runtime_inventory_single_subscription_streams_provider_snapshot():
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json(
                _frame(
                    "providers.runtime_inventory.provider.subscribe",
                    {"provider": "openai"},
                    request_id="req-runtime",
                    correlation_id="corr-runtime",
                    frame_type="subscribe",
                )
            )

            ack = websocket.receive_json()
            snapshot = websocket.receive_json()

    _assert_ack_frame(ack, action="providers.runtime_inventory.provider.subscribe", request_id="req-runtime", correlation_id="corr-runtime", mode="stream")
    assert snapshot["type"] == "snapshot"
    assert snapshot["data"]["provider"] == "openai"
    assert snapshot["data"]["data"]["provider"] == "openai"


def test_control_ws_invalid_action_returns_terminal_error_envelope():
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json(
                _frame(
                    "local_llm/restart",
                    {"model_name": "qwen2.5:0.5b"},
                    request_id="req-invalid-action",
                    correlation_id="corr-invalid-action",
                )
            )

            terminal = websocket.receive_json()

    assert terminal == {
        "type": "error",
        "request_id": "req-invalid-action",
        "correlation_id": "corr-invalid-action",
        "action": "local_llm/restart",
        "data": {},
        "error": {
            "code": "UNSUPPORTED_ACTION",
            "message": "unsupported action: local_llm/restart",
            "retryable": False,
            "category": "routing",
        },
        "final": True,
        "ack": False,
    }


def test_control_ws_invalid_frame_returns_protocol_error_without_ack():
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json(
                {
                    "type": "command",
                    "request_id": "req-invalid-frame",
                    "action": "local_llm/connect",
                    "data": "not-an-object",
                }
            )

            message = websocket.receive_json()

    assert message == {
        "type": "error",
        "request_id": "req-invalid-frame",
        "correlation_id": None,
        "action": "local_llm/connect",
        "data": {},
        "error": {
            "code": "INVALID_FRAME",
            "message": "data must be a JSON object",
            "retryable": False,
            "category": "protocol",
        },
        "final": True,
        "ack": False,
    }


def test_control_ws_operator_introspection_actions_return_terminal_payloads(monkeypatch):
    monkeypatch.setattr(
        antigravity_status_module,
        "shared_antigravity_snapshot",
        lambda force=False: {"status": "ok", "provider": "antigravity", "force": force},
    )
    monkeypatch.setattr(
        openai_bazzite_endpoint,
        "load_openai_endpoint_discovery",
        lambda: {"base_url": "http://openai.local", "models_path": "/v1/models"},
    )

    orch = _FakeControlPlaneOrchestrator()
    app = _build_http_app(orch)
    cases = [
        (
            "stats.get",
            {},
            lambda payload: payload["data"]["status"] == "success" and payload["data"]["data"]["model_usage"]["requests"] == 7,
        ),
        (
            "health.local_models.get",
            {},
            lambda payload: payload["data"]["status"] == "ok" and payload["data"]["memory_pressure"]["pressure_state"] == "normal",
        ),
        (
            "providers.openai.runtime_inventory.get",
            {"force_refresh": True, "probe_limit": 2},
            lambda payload: payload["data"]["status"] == "ok" and payload["data"]["data"]["provider"] == "openai",
        ),
        (
            "providers.openai.discovery.get",
            {},
            lambda payload: payload["data"]["data"]["base_url"] == "http://openai.local",
        ),
        (
            "providers.openai.model_templates.get",
            {"force_refresh": True, "probe_limit": 2},
            lambda payload: payload["data"]["data"] == {"chat": ["gpt-5.5"]},
        ),
        (
            "antigravity.status.get",
            {},
            lambda payload: payload["data"]["provider"] == "antigravity",
        ),
        (
            "memory.dump.get",
            {},
            lambda payload: payload["data"]["modules"] == ["local_llm", "sourcecraft"],
        ),
    ]

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            for index, (action, data, assertion) in enumerate(cases, start=1):
                request_id = f"req-ops-{index}"
                correlation_id = f"corr-ops-{index}"
                websocket.send_json(_frame(action, data, request_id=request_id, correlation_id=correlation_id))
                terminal = websocket.receive_json()

                assert terminal["type"] == "response"
                assert terminal["request_id"] == request_id
                assert terminal["correlation_id"] == correlation_id
                assert terminal["action"] == action
                assert terminal["error"] is None
                assert terminal["final"] is True
                assert assertion(terminal)



def test_legacy_http_stats_route_advertises_control_ws_migration(monkeypatch):
    monkeypatch.setattr(
        antigravity_status_module,
        "shared_antigravity_snapshot",
        lambda force=False: {"status": "ok", "provider": "antigravity", "force": force},
    )
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        response = client.get("/stats")

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert response.headers["x-control-transport"] == "websocket-primary; compatibility=http"
    assert response.headers["x-control-ws-endpoint"] == "/control/ws"
    assert response.headers["x-control-ws-action"] == "stats.get"
    assert response.headers["link"] == "</control/ws>; rel=alternate"


def test_legacy_http_runtime_inventory_route_advertises_ws_get_and_subscribe_actions():
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        response = client.get("/providers/openai/runtime_inventory", params={"force_refresh": "true", "probe_limit": 2})

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert response.headers["x-control-ws-action"] == "providers.openai.runtime_inventory.get"
    assert response.headers["x-control-ws-subscribe"] == "providers.openai.runtime_inventory.subscribe"
