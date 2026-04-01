import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://127.0.0.1:8080/ws/user1/session1"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected to WebSocket")
            # Wait for the greeting
            greeting = await websocket.recv()
            print(f"Received greeting: {greeting[:100]}...")
            
            # Send a handshake
            await websocket.send(json.dumps({"type": "text", "text": "Neural handshake"}))
            print("Sent handshake")
            
            # Wait for a response
            response = await websocket.recv()
            print(f"Received response: {response[:100]}...")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_ws())
