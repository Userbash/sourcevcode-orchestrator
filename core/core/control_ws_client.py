from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import websockets


JsonDict = dict[str, Any]


def control_ws_url(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        raise ValueError("base_url is required")
    if raw.endswith("/control/ws"):
        return raw

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"invalid base_url: {base_url}")

    if parts.scheme == "http":
        scheme = "ws"
    elif parts.scheme == "https":
        scheme = "wss"
    elif parts.scheme in {"ws", "wss"}:
        scheme = parts.scheme
    else:
        raise ValueError(f"unsupported base_url scheme: {parts.scheme}")

    path = parts.path.rstrip("/")
    if path.endswith("/control/ws"):
        ws_path = path
    else:
        ws_path = f"{path}/control/ws" if path else "/control/ws"
    return urlunsplit((scheme, parts.netloc, ws_path, "", ""))


@dataclass(slots=True)
class ControlWsResult:
    url: str
    request: JsonDict
    ack: JsonDict | None
    frames: list[JsonDict]
    terminal: JsonDict

    def terminal_data(self) -> JsonDict:
        data = self.terminal.get("data")
        return dict(data) if isinstance(data, dict) else {}

    def require_success(self) -> "ControlWsResult":
        terminal_error = self.terminal.get("error")
        if terminal_error:
            if isinstance(terminal_error, dict):
                message = str(terminal_error.get("message") or terminal_error.get("code") or "control ws error")
            else:
                message = str(terminal_error)
            raise RuntimeError(message)
        status = self.terminal_data().get("status")
        if status == "error":
            message = self.terminal_data().get("error") or "control ws error"
            raise RuntimeError(str(message))
        return self


class ControlWsSession:
    def __init__(self, base_url: str, *, timeout_sec: float = 30.0) -> None:
        self.url = control_ws_url(base_url)
        self.timeout_sec = float(timeout_sec)
        self.websocket: Any | None = None

    async def __aenter__(self) -> "ControlWsSession":
        self.websocket = await websockets.connect(
            self.url,
            subprotocols=["chat.v1", "chat.json"],
            open_timeout=self.timeout_sec,
            close_timeout=5,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None

    def build_envelope(
        self,
        action: str,
        data: JsonDict | None = None,
        *,
        frame_type: str = "command",
        ack: bool = True,
        request_id: str | None = None,
        correlation_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> JsonDict:
        envelope: JsonDict = {
            "type": frame_type,
            "request_id": request_id or uuid4().hex,
            "correlation_id": correlation_id or uuid4().hex,
            "action": str(action or "").strip(),
            "ack": bool(ack),
            "data": dict(data or {}),
        }
        if timeout_ms is not None:
            envelope["timeout_ms"] = int(timeout_ms)
        return envelope

    async def send_envelope(self, envelope: JsonDict) -> JsonDict:
        if self.websocket is None:
            raise RuntimeError("control websocket session is not open")
        await self.websocket.send(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False))
        return dict(envelope)

    async def recv_frame(self) -> JsonDict:
        if self.websocket is None:
            raise RuntimeError("control websocket session is not open")
        raw = await asyncio.wait_for(self.websocket.recv(), timeout=self.timeout_sec)
        frame = json.loads(raw)
        if not isinstance(frame, dict):
            raise RuntimeError("control ws returned non-object frame")
        return dict(frame)

    async def recv_until_terminal(self, *, allow_ack: bool = True) -> tuple[JsonDict | None, list[JsonDict], JsonDict]:
        ack_frame: JsonDict | None = None
        frames: list[JsonDict] = []
        while True:
            frame = await self.recv_frame()
            if allow_ack and frame.get("type") == "ack" and ack_frame is None:
                ack_frame = frame
                continue
            frames.append(frame)
            if frame.get("type") == "error" or bool(frame.get("final")):
                return ack_frame, frames, frame

    async def execute(
        self,
        action: str,
        data: JsonDict | None = None,
        *,
        frame_type: str = "command",
        ack: bool = True,
        request_id: str | None = None,
        correlation_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> ControlWsResult:
        envelope = self.build_envelope(
            action,
            data,
            frame_type=frame_type,
            ack=ack,
            request_id=request_id,
            correlation_id=correlation_id,
            timeout_ms=timeout_ms,
        )
        await self.send_envelope(envelope)
        ack_frame, frames, terminal = await self.recv_until_terminal(allow_ack=ack)
        return ControlWsResult(url=self.url, request=envelope, ack=ack_frame, frames=frames, terminal=terminal)

    async def cancel(self, request_id: str, *, correlation_id: str | None = None, ack: bool = True) -> JsonDict:
        envelope = self.build_envelope(
            "",
            {},
            frame_type="cancel",
            ack=ack,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return await self.send_envelope(envelope)

    async def unsubscribe(self, request_id: str, *, correlation_id: str | None = None, ack: bool = True) -> JsonDict:
        envelope = self.build_envelope(
            "",
            {},
            frame_type="unsubscribe",
            ack=ack,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return await self.send_envelope(envelope)


async def run_control_ws_action(
    base_url: str,
    action: str,
    data: JsonDict | None = None,
    *,
    frame_type: str = "command",
    ack: bool = True,
    timeout_sec: float = 30.0,
    request_id: str | None = None,
    correlation_id: str | None = None,
    timeout_ms: int | None = None,
) -> ControlWsResult:
    async with ControlWsSession(base_url, timeout_sec=timeout_sec) as session:
        return await session.execute(
            action,
            data,
            frame_type=frame_type,
            ack=ack,
            request_id=request_id,
            correlation_id=correlation_id,
            timeout_ms=timeout_ms,
        )


def run_control_ws_action_sync(
    base_url: str,
    action: str,
    data: JsonDict | None = None,
    **kwargs: Any,
) -> ControlWsResult:
    return asyncio.run(run_control_ws_action(base_url, action, data, **kwargs))
