import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8080/chat/ws"
    try:
        async with websockets.connect(uri, subprotocols=["chat.json"], open_timeout=30) as websocket:
            print("Connected successfully!")
            test_msg = {
                "user_id": "test_user",
                "message": "ping status",
                "session_id": "test_session",
                "source": "cli_test"
            }
            await websocket.send(json.dumps(test_msg))
            
            while True:
                response = await asyncio.wait_for(websocket.recv(), timeout=60)
                data = json.loads(response)
                print(f"WS Received: {data}")
                if data.get("type") == "final_result":
                    break
    except Exception as e:
        print(f"WS Failed: {e}")

asyncio.run(test_ws())
