from __future__ import annotations

import argparse
import asyncio
import json

import websockets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WebSocket smoke test for the orchestrator chat bridge")
    parser.add_argument("--url", default="ws://localhost:8000/chat/ws", help="WebSocket URL")
    parser.add_argument("--user-id", default="smoke-user", help="User identifier")
    parser.add_argument("--session-id", default="smoke-session", help="Session identifier")
    parser.add_argument("--message", default="ping", help="Message to submit")
    return parser


async def run_smoke(url: str, *, user_id: str, session_id: str, message: str) -> int:
    payload = {"c": {"v": 1, "u": user_id, "m": message, "s": session_id, "o": "smoke_test", "p": "cli", "t": 0}}
    async with websockets.connect(url, subprotocols=["chat.v1", "chat.json"], open_timeout=20, close_timeout=5) as websocket:
        await websocket.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        while True:
            response = await asyncio.wait_for(websocket.recv(), timeout=60)
            data = json.loads(response)
            if data.get("type") != "final_result":
                continue
            print(json.dumps({"status": data.get("status"), "task_id": data.get("task_id"), "transport": data.get("delivery", {}).get("transport"), "endpoint": data.get("delivery", {}).get("endpoint")}, ensure_ascii=False))
            return 0 if data.get("status") == "completed" else 1


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run_smoke(args.url, user_id=args.user_id, session_id=args.session_id, message=args.message))


if __name__ == "__main__":
    raise SystemExit(main())
