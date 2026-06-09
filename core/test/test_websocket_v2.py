import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8000/chat/ws"
    # Попробуем с указанием subprotocol, так как код сервера его ожидает
    try:
        async with websockets.connect(uri, subprotocols=["chat.json"]) as websocket:
            print(f"Connected to {uri} with subprotocol 'chat.json'")
            
            # Send a ping-like test message
            test_msg = {
                "user_id": "test_user",
                "message": "ping",
                "session_id": "test_session",
                "source": "cli_test"
            }
            await websocket.send(json.dumps(test_msg))
            print(f"Sent: {test_msg}")
            
            # Wait for response
            response = await websocket.recv()
            print(f"Received: {response}")
            
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_ws())
