import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8000/chat/ws"
    # Увеличен таймаут и логирование handshake
    try:
        async with websockets.connect(uri, subprotocols=["chat.json"], open_timeout=30) as websocket:
            print("Connected successfully!")
            test_msg = {
                "user_id": "test_user",
                "message": "ping",
                "session_id": "test_session",
                "source": "cli_test"
            }
            await websocket.send(json.dumps(test_msg))
            response = await asyncio.wait_for(websocket.recv(), timeout=10)
            print(f"Received: {response}")
            
    except asyncio.TimeoutError:
        print("Timeout waiting for response")
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_ws())
