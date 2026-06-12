from __future__ import annotations

import asyncio
import json
import time

import websockets


def _frame(message: str, *, session_id: str) -> dict[str, object]:
    return {"c": {"v": 1, "u": "tester", "m": message, "s": session_id, "o": "smart_features", "p": "cli", "t": 0}}


async def verify_smart_features():
    print("=== AI Orchestrator Smart Features Verification ===")
    uri = "ws://localhost:8000/chat/ws"

    print("[*] Populating memory with a simple task...")
    async with websockets.connect(uri, subprotocols=["chat.v1", "chat.json"], open_timeout=30) as websocket:
        await websocket.send(json.dumps(_frame("RESEARCH: Memory implementation patterns", session_id="smart-session"), separators=(",", ":"), ensure_ascii=False))
        while True:
            data = json.loads(await asyncio.wait_for(websocket.recv(), timeout=60))
            if data.get("type") == "final_result":
                break

    print("[*] Testing Prompt Optimization & Smart Decomposition...")
    async with websockets.connect(uri, subprotocols=["chat.v1", "chat.json"], open_timeout=30) as websocket:
        await websocket.send(json.dumps(_frame("PLAN: Build a complex caching layer using the patterns found in previous research.", session_id="smart-session"), separators=(",", ":"), ensure_ascii=False))
        while True:
            data = json.loads(await asyncio.wait_for(websocket.recv(), timeout=90))
            if data.get("type") != "final_result":
                continue
            print(f"[+] Status: {data.get('status')}")
            print("[!] Check Orchestrator logs for [OPTIMIZER] and [DECOMP] tags.")
            break


if __name__ == "__main__":
    time.sleep(5)
    asyncio.run(verify_smart_features())
