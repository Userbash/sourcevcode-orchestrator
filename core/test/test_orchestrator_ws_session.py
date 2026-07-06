from __future__ import annotations

import asyncio

import pytest

from core.core.orchestrator_ws_session import (
    OrchestratorWebSocketSession,
    SessionAuthError,
    SessionBackpressureError,
    build_ws_envelope,
    negotiate_subprotocol,
)


class _FakeWebSocket:
    def __init__(self, headers=None, *, send_delay_sec: float = 0.0):
        self.headers = headers or {}
        self.send_delay_sec = send_delay_sec
        self.accepted_subprotocol = None
        self.accept_calls = 0
        self.close_calls: list[dict[str, object]] = []
        self.sent_frames: list[dict] = []

    async def accept(self, subprotocol=None):
        self.accept_calls += 1
        self.accepted_subprotocol = subprotocol

    async def send_json(self, data):
        if self.send_delay_sec > 0:
            await asyncio.sleep(self.send_delay_sec)
        self.sent_frames.append(data)

    async def close(self, code=1000, reason=None):
        self.close_calls.append({"code": code, "reason": reason})


def test_negotiate_subprotocol_picks_first_supported_value():
    protocol = negotiate_subprotocol(
        {"sec-websocket-protocol": "binary.v1, chat.json, chat.v1"},
    )

    assert protocol == "chat.json"


def test_build_ws_envelope_keeps_unified_contract_shape():
    frame = build_ws_envelope(
        type="event",
        request_id="req-1",
        correlation_id="corr-1",
        action="runtime.subscribe",
        data={"ok": True},
        error=None,
        final=False,
    )

    assert frame == {
        "type": "event",
        "request_id": "req-1",
        "correlation_id": "corr-1",
        "action": "runtime.subscribe",
        "data": {"ok": True},
        "error": None,
        "final": False,
    }


def test_accept_negotiates_subprotocol_and_runs_auth_placeholder():
    async def _run():
        websocket = _FakeWebSocket(
            {"sec-websocket-protocol": "chat.v1, chat.json", "authorization": "Bearer token-1"}
        )

        async def auth_handler(ws):
            return {"subject": ws.headers["authorization"], "authenticated": True}

        session = OrchestratorWebSocketSession(websocket, auth_handler=auth_handler, session_id="ws-1")
        result = await session.accept()
        return websocket, result, session

    websocket, result, session = asyncio.run(_run())

    assert websocket.accept_calls == 1
    assert websocket.accepted_subprotocol == "chat.v1"
    assert result.session_id == "ws-1"
    assert result.subprotocol == "chat.v1"
    assert result.principal["authenticated"] is True
    assert session.principal["subject"] == "Bearer token-1"


def test_accept_closes_socket_when_auth_fails():
    async def _run():
        websocket = _FakeWebSocket({"sec-websocket-protocol": "chat.v1"})

        async def auth_handler(_ws):
            raise SessionAuthError("bad token")

        session = OrchestratorWebSocketSession(websocket, auth_handler=auth_handler)
        with pytest.raises(SessionAuthError):
            await session.accept()
        return websocket

    websocket = asyncio.run(_run())

    assert websocket.close_calls == [{"code": 4401, "reason": "unauthorized"}]


def test_send_response_writes_envelope():
    async def _run():
        websocket = _FakeWebSocket({"sec-websocket-protocol": "chat.v1"})
        session = OrchestratorWebSocketSession(websocket, session_id="ws-2")
        await session.send_response(
            type="event",
            request_id="req-2",
            correlation_id="corr-2",
            action="providers.snapshot",
            data={"providers": 3},
            final=True,
        )
        return websocket

    websocket = asyncio.run(_run())

    assert websocket.sent_frames == [
        {
            "type": "event",
            "request_id": "req-2",
            "correlation_id": "corr-2",
            "action": "providers.snapshot",
            "data": {"providers": 3},
            "error": None,
            "final": True,
        }
    ]


def test_send_frame_raises_backpressure_error_on_timeout_and_closes_socket():
    async def _run():
        websocket = _FakeWebSocket(send_delay_sec=0.05)
        session = OrchestratorWebSocketSession(websocket, send_timeout_sec=0.001)
        with pytest.raises(SessionBackpressureError):
            await session.send_frame({"type": "event"})
        return websocket

    websocket = asyncio.run(_run())

    assert websocket.close_calls == [{"code": 1011, "reason": "send_timeout"}]


def test_handle_ping_frame_returns_pong():
    async def _run():
        websocket = _FakeWebSocket()
        session = OrchestratorWebSocketSession(websocket, session_id="ws-3")
        handled = await session.handle_control_frame(
            {"type": "ping", "request_id": "req-3", "correlation_id": "corr-3", "action": "control.ping"}
        )
        return handled, websocket

    handled, websocket = asyncio.run(_run())

    assert handled is True
    assert websocket.sent_frames[0]["type"] == "pong"
    assert websocket.sent_frames[0]["request_id"] == "req-3"
    assert websocket.sent_frames[0]["correlation_id"] == "corr-3"
    assert websocket.sent_frames[0]["action"] == "control.ping"


def test_cancel_control_frame_cancels_tracked_request_and_sends_ack():
    async def _run():
        websocket = _FakeWebSocket()
        session = OrchestratorWebSocketSession(websocket)

        async def pending():
            await asyncio.sleep(60)

        task = asyncio.create_task(pending())
        session.track_request("req-4", task)
        handled = await session.handle_control_frame({"type": "cancel", "request_id": "req-4", "action": "cancel"})
        return handled, task.cancelled(), websocket, session

    handled, cancelled, websocket, session = asyncio.run(_run())

    assert handled is True
    assert cancelled is True
    assert "req-4" not in session.active_request_ids
    assert websocket.sent_frames[0]["type"] == "ack"
    assert websocket.sent_frames[0]["data"]["cancelled"] is True


def test_subscription_registry_hooks_and_cleanup_run_disconnect_logic():
    async def _run():
        events: list[tuple[str, str]] = []
        websocket = _FakeWebSocket()

        async def on_subscribe(_session, binding):
            events.append(("subscribe", binding.subscription_id))

        async def on_unsubscribe(_session, binding):
            events.append(("unsubscribe", binding.subscription_id))

        async def on_disconnect(session):
            events.append(("disconnect", session.session_id))

        async def unsubscribe():
            events.append(("cleanup", "sub-1"))

        session = OrchestratorWebSocketSession(
            websocket,
            session_id="ws-4",
            on_subscribe=on_subscribe,
            on_unsubscribe=on_unsubscribe,
            on_disconnect=on_disconnect,
        )
        await session.register_subscription("sub-1", topic="runtime_events", unsubscribe=unsubscribe)
        removed = await session.handle_control_frame(
            {
                "type": "unsubscribe",
                "request_id": "req-5",
                "data": {"subscription_id": "sub-1"},
            }
        )
        await session.register_subscription("sub-2", topic="inventory")
        await session.close(code=1001, reason="client_gone")
        return removed, events, websocket, session

    removed, events, websocket, session = asyncio.run(_run())

    assert removed is True
    assert ("subscribe", "sub-1") in events
    assert ("cleanup", "sub-1") in events
    assert ("unsubscribe", "sub-1") in events
    assert ("unsubscribe", "sub-2") in events
    assert ("disconnect", "ws-4") in events
    assert websocket.close_calls == [{"code": 1001, "reason": "client_gone"}]
    assert session.subscriptions == {}

