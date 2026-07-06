from __future__ import annotations

import asyncio

from core.core.orchestrator_ws_dispatcher import OrchestratorWSDispatcher
from core.core.ws_protocol import build_request


class _FakeSocratiCodeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def status_payload(self, request):
        self.calls.append(("status", dict(request.data)))
        return {
            "status": "ok",
            "data": {
                "provider": "socraticode",
                "status": "ready",
                "ready": True,
                "session_id": "soc-1",
            },
        }, 200

    def context_readiness_payload(self, request):
        self.calls.append(("context_readiness", dict(request.data)))
        return {
            "status": "ok",
            "data": {
                "provider": "socraticode",
                "context_ready": False,
                "missing": ["repo_map"],
                "reason": "indexing",
            },
        }, 503


def _build_dispatcher(fake: _FakeSocratiCodeTransport) -> OrchestratorWSDispatcher:
    dispatcher = OrchestratorWSDispatcher()
    dispatcher.register_single("socraticode.status.get", fake.status_payload)
    dispatcher.register_single("socraticode.context_readiness.get", fake.context_readiness_payload)
    return dispatcher


async def _collect(dispatcher: OrchestratorWSDispatcher, request: dict) -> list[dict]:
    rows = []
    async for item in dispatcher.dispatch(request):
        rows.append(item)
    return rows


def test_socraticode_status_http_query_returns_handler_payload_and_status_code():
    fake = _FakeSocratiCodeTransport()
    dispatcher = _build_dispatcher(fake)

    payload, status_code = asyncio.run(
        dispatcher.execute_http(
            build_request(
                "socraticode.status.get",
                request_id="req-status-http",
                data={"workspace_id": "ws-1"},
            )
        )
    )

    assert status_code == 200
    assert payload == {
        "status": "ok",
        "data": {
            "provider": "socraticode",
            "status": "ready",
            "ready": True,
            "session_id": "soc-1",
        },
    }
    assert fake.calls == [("status", {"workspace_id": "ws-1"})]


def test_socraticode_context_readiness_http_query_returns_handler_payload_and_status_code():
    fake = _FakeSocratiCodeTransport()
    dispatcher = _build_dispatcher(fake)

    payload, status_code = asyncio.run(
        dispatcher.execute_http(
            build_request(
                "socraticode.context_readiness.get",
                request_id="req-context-http",
                data={"workspace_id": "ws-1", "conversation_id": "conv-7"},
            )
        )
    )

    assert status_code == 503
    assert payload == {
        "status": "ok",
        "data": {
            "provider": "socraticode",
            "context_ready": False,
            "missing": ["repo_map"],
            "reason": "indexing",
        },
    }
    assert fake.calls == [("context_readiness", {"workspace_id": "ws-1", "conversation_id": "conv-7"})]


def test_socraticode_status_ws_query_returns_single_terminal_response_without_ack():
    fake = _FakeSocratiCodeTransport()
    dispatcher = _build_dispatcher(fake)

    rows = asyncio.run(
        _collect(
            dispatcher,
            build_request(
                "socraticode.status.get",
                request_id="req-status-ws",
                correlation_id="corr-status-ws",
                ack=True,
                data={"workspace_id": "ws-1"},
            ),
        )
    )

    assert rows == [
        {
            "type": "response",
            "request_id": "req-status-ws",
            "correlation_id": "corr-status-ws",
            "action": "socraticode.status.get",
            "data": {
                "status": "ok",
                "data": {
                    "provider": "socraticode",
                    "status": "ready",
                    "ready": True,
                    "session_id": "soc-1",
                },
            },
            "error": None,
            "final": True,
            "ack": False,
        }
    ]
    assert fake.calls == [("status", {"workspace_id": "ws-1"})]


def test_socraticode_context_readiness_ws_query_wraps_non_200_payload_as_terminal_response():
    fake = _FakeSocratiCodeTransport()
    dispatcher = _build_dispatcher(fake)

    rows = asyncio.run(
        _collect(
            dispatcher,
            build_request(
                "socraticode.context_readiness.get",
                request_id="req-context-ws",
                correlation_id="corr-context-ws",
                ack=True,
                data={"workspace_id": "ws-1", "conversation_id": "conv-7"},
            ),
        )
    )

    assert rows == [
        {
            "type": "response",
            "request_id": "req-context-ws",
            "correlation_id": "corr-context-ws",
            "action": "socraticode.context_readiness.get",
            "data": {
                "status": "ok",
                "data": {
                    "provider": "socraticode",
                    "context_ready": False,
                    "missing": ["repo_map"],
                    "reason": "indexing",
                },
            },
            "error": None,
            "final": True,
            "ack": False,
        }
    ]
    assert fake.calls == [("context_readiness", {"workspace_id": "ws-1", "conversation_id": "conv-7"})]
