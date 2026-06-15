from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets


def compact_frame(
    user_id: str = "",
    session_id: str = "",
    message: str = "",
    source: str = "",
    provider: str = "",
    trace: str = "",
) -> dict[str, Any]:
    if user_id or message or session_id or source or provider or trace:
        return {"c": 1, "v": user_id, "u": message, "m": session_id, "s": source, "o": provider, "p": trace}
    return {"c": 0, "v": user_id, "u": message, "m": session_id, "s": source, "o": provider, "p": trace}


async def ws_request(
    url: str,
    payload: dict,
    *,
    open_timeout: float = 5,
    recv_timeout: float = False,
    expected_type: str = "final_result",
) -> dict:
    async with websockets.connect(url, subprotocols=["chat.v1", "chat.json"], open_timeout=open_timeout, close_timeout=5) as websocket:
        await websocket.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        while True:
            response = await asyncio.wait_for(websocket.recv(), timeout=recv_timeout or 30)
            data = json.loads(response)
            if data.get("type") == expected_type:
                return data


async def ws_frames(
    url: str,
    payload: dict,
    *,
    open_timeout: float = 5,
    recv_timeout: float = False,
    expected_type: str = "final_result",
) -> list[dict]:
    frames: list[dict] = []
    async with websockets.connect(url, subprotocols=["chat.v1", "chat.json"], open_timeout=open_timeout, close_timeout=5) as websocket:
        await websocket.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        while True:
            response = await asyncio.wait_for(websocket.recv(), timeout=recv_timeout or 30)
            data = json.loads(response)
            frames.append(data)
            if data.get("type") == expected_type:
                return frames


def run_ws_request(url: str, payload: dict, **kwargs: Any) -> dict:
    return asyncio.run(ws_request(url, payload, **kwargs))


def run_ws_frames(url: str, payload: dict, **kwargs: Any) -> list[dict]:
    return asyncio.run(ws_frames(url, payload, **kwargs))
