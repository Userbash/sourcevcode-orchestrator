import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://localhost:8000/chat/ws"
    try:
        async with websockets.connect(uri) as websocket:
            print(f"Connected to {uri}")
            
            # Send a simple ping-like test message
            test_msg = {"type": "ping", "data": "hello"}
            await websocket.send(json.dumps(test_msg))
            print(f"Sent: {test_msg}")
            
            # Wait for response
            response = await websocket.recv()
            print(f"Received: {response}")
            
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_ws())
