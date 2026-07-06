from __future__ import annotations

import asyncio

from core.core.orchestrator_ws_dispatcher import OrchestratorTransportRequest, OrchestratorWSDispatcher, build_orchestrator_ws_dispatcher
from core.core.ws_protocol import build_request


class _FakeOrchestrator:
    pass


async def _collect(dispatcher, request):
    rows = []
    async for item in dispatcher.dispatch(request):
        rows.append(item)
    return rows


def test_dispatcher_rejects_unknown_action():
    dispatcher = OrchestratorWSDispatcher()

    rows = asyncio.run(
        _collect(
            dispatcher,
            build_request("missing.action", request_id="req-1"),
        )
    )

    assert rows == [
        {
            "type": "error",
            "request_id": "req-1",
            "correlation_id": None,
            "action": "missing.action",
            "data": {},
            "error": {
                "code": "UNSUPPORTED_ACTION",
                "message": "unsupported action: missing.action",
                "retryable": False,
                "category": "routing",
            },
            "final": True,
            "ack": False,
        }
    ]


def test_dispatcher_wraps_single_response_handler():
    dispatcher = OrchestratorWSDispatcher()

    def _handler(request):
        assert request.data == {"model_name": "qwen"}
        return {"status": "ok", "data": {"connected": True, "model_name": "qwen"}}, 200

    dispatcher.register_single("providers.local_llm.connect", _handler, send_ack=True)

    rows = asyncio.run(
        _collect(
            dispatcher,
            build_request(
                "providers.local_llm.connect",
                request_id="req-2",
                correlation_id="corr-2",
                ack=True,
                data={"model_name": "qwen"},
            ),
        )
    )

    assert rows == [
        {
            "type": "ack",
            "request_id": "req-2",
            "correlation_id": "corr-2",
            "action": "providers.local_llm.connect",
            "data": {
                "accepted": True,
                "mode": "single",
                "idempotency_key": None,
                "timeout_ms": None,
            },
            "error": None,
            "final": False,
            "ack": True,
        },
        {
            "type": "response",
            "request_id": "req-2",
            "correlation_id": "corr-2",
            "action": "providers.local_llm.connect",
            "data": {"status": "ok", "data": {"connected": True, "model_name": "qwen"}},
            "error": None,
            "final": True,
            "ack": False,
        },
    ]


def test_dispatcher_wraps_async_stream_with_ack_and_terminal_event():
    dispatcher = OrchestratorWSDispatcher()

    async def _stream_handler(request):
        assert request.idempotency_key == "idem-3"

        async def _iterator():
            yield {"type": "event", "data": {"step": "started"}}
            yield {"type": "event", "data": {"step": "finished"}, "final": True}

        return _iterator()

    dispatcher.register_stream("runtime.events.subscribe", _stream_handler)

    rows = asyncio.run(
        _collect(
            dispatcher,
            OrchestratorTransportRequest.from_dict(
                build_request(
                    "runtime.events.subscribe",
                    request_id="req-3",
                    correlation_id="corr-3",
                    ack=True,
                    idempotency_key="idem-3",
                )
            ),
        )
    )

    assert rows == [
        {
            "type": "ack",
            "request_id": "req-3",
            "correlation_id": "corr-3",
            "action": "runtime.events.subscribe",
            "data": {
                "accepted": True,
                "mode": "stream",
                "idempotency_key": "idem-3",
                "timeout_ms": None,
            },
            "error": None,
            "final": False,
            "ack": True,
        },
        {
            "type": "event",
            "request_id": "req-3",
            "correlation_id": "corr-3",
            "action": "runtime.events.subscribe",
            "data": {"type": "event", "data": {"step": "started"}},
            "error": None,
            "final": False,
            "ack": False,
        },
        {
            "type": "event",
            "request_id": "req-3",
            "correlation_id": "corr-3",
            "action": "runtime.events.subscribe",
            "data": {"type": "event", "data": {"step": "finished"}, "final": True},
            "error": None,
            "final": True,
            "ack": False,
        },
    ]


def test_execute_http_reuses_single_handler_contract():
    dispatcher = OrchestratorWSDispatcher()

    dispatcher.register_single("transport.audit.get", lambda request: ({"status": "ok", "data": {"shape": request.type}}, 200))

    payload, status_code = asyncio.run(
        dispatcher.execute_http(build_request("transport.audit.get", request_id="req-4", data={}))
    )

    assert status_code == 200
    assert payload == {"status": "ok", "data": {"shape": "command"}}


def test_default_dispatcher_registers_transport_actions():
    dispatcher = build_orchestrator_ws_dispatcher(_FakeOrchestrator())

    assert dispatcher.has_action("providers.local_llm.connect")
    assert dispatcher.has_action("providers.runtime_inventory.subscribe")
    assert dispatcher.has_action("providers.ai_kernel.ensure")
    assert dispatcher.has_action("diagnostics.get")
