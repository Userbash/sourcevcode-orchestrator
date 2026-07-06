from __future__ import annotations

from fastapi.testclient import TestClient

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
        return {
            "provider": "local_llm",
            "status": "ready",
            "models": [{"model_name": "qwen2.5:0.5b"}],
            "summary": {"total_models": 1},
        }


class _FakeBudgetRouter:
    def suppression_snapshot(self):
        return {}


class _FakeUsageModule:
    def finalize(self):
        return {}


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


class _FakeModuleManager:
    def __init__(self) -> None:
        self.local_llm = _FakeLocalLLMModule()

    def get_module(self, name):
        if name == "model_usage":
            return _FakeUsageModule()
        if name == "local_llm":
            return self.local_llm
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


class _FakeControlPlaneOrchestrator:
    def __init__(self) -> None:
        self.provider_inventory = _FakeProviderInventory()
        self.provider_budget_router = _FakeBudgetRouter()
        self.module_manager = _FakeModuleManager()
        self.ai_kernel_bridge = _FakeAIKernelBridge()

    def get_module(self, name):
        return self.module_manager.get_module(name)


def _command_frame(action: str, data: dict, *, request_id: str = "req-1", correlation_id: str = "corr-1") -> dict:
    return {
        "type": "command",
        "request_id": request_id,
        "correlation_id": correlation_id,
        "action": action,
        "data": data,
        "idempotency_key": f"idem-{request_id}",
        "timeout_ms": 5_000,
    }


def _assert_ack_frame(message: dict, *, action: str, request_id: str = "req-1", correlation_id: str = "corr-1") -> None:
    assert message == {
        "type": "ack",
        "request_id": request_id,
        "correlation_id": correlation_id,
        "action": action,
        "data": {"accepted": True},
        "error": None,
        "final": False,
    }


def test_control_ws_local_llm_commands_emit_ack_then_terminal_response():
    orch = _FakeControlPlaneOrchestrator()
    app = _build_http_app(orch)

    cases = [
        (
            "local_llm/connect",
            {"model_name": "qwen2.5:0.5b"},
            lambda payload: payload["data"]["connected"] is True and payload["data"]["model_name"] == "qwen2.5:0.5b",
        ),
        (
            "local_llm/disconnect",
            {"model_name": "qwen2.5:0.5b"},
            lambda payload: payload["data"]["disconnected"] is True and payload["data"]["model_name"] == "qwen2.5:0.5b",
        ),
        (
            "local_llm/warm",
            {"model_name": "qwen2.5:0.5b", "keep_alive": 120, "timeout_sec": 2.0},
            lambda payload: payload["data"]["warmed"] is True and payload["data"]["result"]["model"] == "qwen2.5:0.5b",
        ),
    ]

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            for index, (action, data, assertion) in enumerate(cases, start=1):
                request_id = f"req-{index}"
                correlation_id = f"corr-{index}"
                websocket.send_json(_command_frame(action, data, request_id=request_id, correlation_id=correlation_id))

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
                _command_frame(
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
    assert terminal["data"]["ready"] is True
    assert terminal["data"]["attempted_autostart"] is True
    assert terminal["data"]["orchestrator_usable_models"] == ["hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"]
    assert orch.ai_kernel_bridge.calls == [
        {
            "model_name": "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
            "ensure_ready": True,
        }
    ]


def test_control_ws_invalid_action_returns_terminal_error_envelope():
    app = _build_http_app(_FakeControlPlaneOrchestrator())

    with TestClient(app) as client:
        with client.websocket_connect("/control/ws", subprotocols=["chat.v1", "chat.json"]) as websocket:
            websocket.send_json(
                _command_frame(
                    "local_llm/restart",
                    {"model_name": "qwen2.5:0.5b"},
                    request_id="req-invalid-action",
                    correlation_id="corr-invalid-action",
                )
            )

            ack = websocket.receive_json()
            terminal = websocket.receive_json()

    _assert_ack_frame(
        ack,
        action="local_llm/restart",
        request_id="req-invalid-action",
        correlation_id="corr-invalid-action",
    )
    assert terminal == {
        "type": "error",
        "request_id": "req-invalid-action",
        "correlation_id": "corr-invalid-action",
        "action": "local_llm/restart",
        "data": None,
        "error": {
            "code": "invalid_action",
            "message": "Unsupported control action: local_llm/restart",
        },
        "final": True,
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
                }
            )

            message = websocket.receive_json()

    assert message == {
        "type": "error",
        "request_id": "req-invalid-frame",
        "correlation_id": None,
        "action": "local_llm/connect",
        "data": None,
        "error": {
            "code": "invalid_frame",
            "message": "Invalid control frame",
        },
        "final": True,
    }
