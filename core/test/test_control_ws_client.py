from __future__ import annotations

import asyncio
import json

import pytest

from core.core.control_ws_client import ControlWsSession, control_ws_url, run_control_ws_action


class _FakeWebSocket:
    def __init__(self, frames: list[dict[str, object]]) -> None:
        self._frames = [json.dumps(frame) for frame in frames]
        self.sent_messages: list[dict[str, object]] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent_messages.append(json.loads(payload))

    async def recv(self) -> str:
        if not self._frames:
            raise RuntimeError("no more frames")
        return self._frames.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:8000", "ws://127.0.0.1:8000/control/ws"),
        ("https://core.example.com/api", "wss://core.example.com/api/control/ws"),
        ("ws://127.0.0.1:8000", "ws://127.0.0.1:8000/control/ws"),
        ("wss://core.example.com/control/ws", "wss://core.example.com/control/ws"),
    ],
)
def test_control_ws_url_normalizes_transport(base_url: str, expected: str):
    assert control_ws_url(base_url) == expected


async def _connect_stub(websocket: _FakeWebSocket, *args, **kwargs) -> _FakeWebSocket:
    return websocket


def test_run_control_ws_action_collects_ack_and_terminal(monkeypatch: pytest.MonkeyPatch):
    websocket = _FakeWebSocket(
        [
            {
                "type": "ack",
                "request_id": "req-1",
                "correlation_id": "corr-1",
                "action": "stats.get",
                "data": {"accepted": True, "mode": "single"},
                "error": None,
                "final": False,
            },
            {
                "type": "response",
                "request_id": "req-1",
                "correlation_id": "corr-1",
                "action": "stats.get",
                "data": {"status": "success", "data": {"provider_inventory": {}}},
                "error": None,
                "final": True,
            },
        ]
    )
    monkeypatch.setattr("core.core.control_ws_client.websockets.connect", lambda *args, **kwargs: _connect_stub(websocket, *args, **kwargs))

    result = asyncio.run(run_control_ws_action("http://127.0.0.1:8000", "stats.get", {"force_refresh": True}, request_id="req-1", correlation_id="corr-1"))

    assert result.url == "ws://127.0.0.1:8000/control/ws"
    assert result.ack is not None
    assert result.ack["type"] == "ack"
    assert result.terminal["type"] == "response"
    assert result.terminal_data()["status"] == "success"
    assert websocket.sent_messages == [
        {
            "type": "command",
            "request_id": "req-1",
            "correlation_id": "corr-1",
            "action": "stats.get",
            "ack": True,
            "data": {"force_refresh": True},
        }
    ]
    assert websocket.closed is True


def test_control_ws_session_supports_subscribe_cancel_and_unsubscribe(monkeypatch: pytest.MonkeyPatch):
    websocket = _FakeWebSocket(
        [
            {
                "type": "ack",
                "request_id": "req-sub",
                "correlation_id": "corr-sub",
                "action": "diagnostics.subscribe",
                "data": {"accepted": True, "mode": "stream"},
                "error": None,
                "final": False,
            },
            {
                "type": "event",
                "request_id": "req-sub",
                "correlation_id": "corr-sub",
                "action": "diagnostics.subscribe",
                "data": {"stage": "completed", "payload": {"status": "ok"}},
                "error": None,
                "final": True,
            },
        ]
    )
    monkeypatch.setattr("core.core.control_ws_client.websockets.connect", lambda *args, **kwargs: _connect_stub(websocket, *args, **kwargs))

    async def _run() -> tuple[dict[str, object], dict[str, object], object]:
        async with ControlWsSession("http://127.0.0.1:8000", timeout_sec=5.0) as session:
            result = await session.execute(
                "diagnostics.subscribe",
                {"layers": ["providers"]},
                frame_type="subscribe",
                request_id="req-sub",
                correlation_id="corr-sub",
                timeout_ms=10_000,
            )
            cancel_frame = await session.cancel("req-sub", correlation_id="corr-sub")
            unsubscribe_frame = await session.unsubscribe("req-sub", correlation_id="corr-sub")
            return cancel_frame, unsubscribe_frame, result

    cancel_frame, unsubscribe_frame, result = asyncio.run(_run())

    assert result.ack is not None
    assert result.ack["type"] == "ack"
    assert result.terminal["type"] == "event"
    assert result.terminal["final"] is True
    assert cancel_frame["type"] == "cancel"
    assert unsubscribe_frame["type"] == "unsubscribe"
    assert websocket.sent_messages == [
        {
            "type": "subscribe",
            "request_id": "req-sub",
            "correlation_id": "corr-sub",
            "action": "diagnostics.subscribe",
            "ack": True,
            "data": {"layers": ["providers"]},
            "timeout_ms": 10000,
        },
        {
            "type": "cancel",
            "request_id": "req-sub",
            "correlation_id": "corr-sub",
            "action": "",
            "ack": True,
            "data": {},
        },
        {
            "type": "unsubscribe",
            "request_id": "req-sub",
            "correlation_id": "corr-sub",
            "action": "",
            "ack": True,
            "data": {},
        },
    ]


def test_run_control_ws_action_require_success_raises_on_terminal_error(monkeypatch: pytest.MonkeyPatch):
    websocket = _FakeWebSocket(
        [
            {
                "type": "error",
                "request_id": "req-2",
                "correlation_id": "corr-2",
                "action": "diagnostics.subscribe",
                "data": {},
                "error": {"code": "UNSUPPORTED_ACTION", "message": "unsupported action"},
                "final": True,
            }
        ]
    )
    monkeypatch.setattr("core.core.control_ws_client.websockets.connect", lambda *args, **kwargs: _connect_stub(websocket, *args, **kwargs))

    result = asyncio.run(
        run_control_ws_action(
            "http://127.0.0.1:8000",
            "diagnostics.subscribe",
            {"layers": ["transport"]},
            frame_type="subscribe",
            request_id="req-2",
            correlation_id="corr-2",
        )
    )

    with pytest.raises(RuntimeError, match="unsupported action"):
        result.require_success()
