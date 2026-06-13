from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets


def compact_frame(*, user_id: str, session_id: str, message: str, source: str = "cli_test", provider: str = "test", trace: bool = False) -> dict[str, Any]:
    return {"c": {"v": 1, "u": user_id, "m": message, "s": session_id, "o": source, "p": provider, "t": 1 if trace else 0}}


async def ws_request(url: str, payload: dict[str, Any], *, open_timeout: int = 30, recv_timeout: int = 60) -> dict[str, Any]:
    async with websockets.connect(url, subprotocols=["chat.v1", "chat.json"], open_timeout=open_timeout, close_timeout=5) as websocket:
        await websocket.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        while True:
            response = await asyncio.wait_for(websocket.recv(), timeout=recv_timeout)
            data = json.loads(response)
            if data.get("type") == "final_result":
                return data


async def ws_frames(url: str, payload: dict[str, Any], *, open_timeout: int = 30, recv_timeout: int = 60) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    async with websockets.connect(url, subprotocols=["chat.v1", "chat.json"], open_timeout=open_timeout, close_timeout=5) as websocket:
        await websocket.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        while True:
            response = await asyncio.wait_for(websocket.recv(), timeout=recv_timeout)
            data = json.loads(response)
            frames.append(data)
            if data.get("type") == "final_result":
                return frames


def run_ws_request(url: str, payload: dict[str, Any], *, open_timeout: int = 30, recv_timeout: int = 60) -> dict[str, Any]:
    return asyncio.run(ws_request(url, payload, open_timeout=open_timeout, recv_timeout=recv_timeout))


def run_ws_frames(url: str, payload: dict[str, Any], *, open_timeout: int = 30, recv_timeout: int = 60) -> list[dict[str, Any]]:
    return asyncio.run(ws_frames(url, payload, open_timeout=open_timeout, recv_timeout=recv_timeout))
