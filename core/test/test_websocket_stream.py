import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8000/chat/ws"
    try:
        async with websockets.connect(uri, subprotocols=["chat.json"], open_timeout=30) as websocket:
            print("Connected successfully!")
            test_msg = {
                "user_id": "test_user",
                "message": "tell me a 5 word joke",
                "session_id": "test_session",
                "source": "cli_test",
                "project": "wisper"
            }
            await websocket.send(json.dumps(test_msg))
            
            while True:
                response = await asyncio.wait_for(websocket.recv(), timeout=30)
                data = json.loads(response)
                if data.get("type") == "stream_event":
                    print(f"[STREAM] {data.get('stage')}: {data.get('message')}")
                elif data.get("type") == "final_result":
                    print(f"[FINAL] Status: {data.get('status')} | {data.get('task_id', 'unknown')}")
                    if data.get("status") == "error":
                        print(f"Error Message: {data.get('message')}")
                    break
                else:
                    print(f"[OTHER] {data}")
            
    except asyncio.TimeoutError:
        print("Timeout waiting for response")
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_ws())
