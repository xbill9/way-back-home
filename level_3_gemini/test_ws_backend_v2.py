import asyncio
import websockets
import json
import pytest


@pytest.mark.anyio
async def test_ws():
    uri = "ws://127.0.0.1:8080/ws/user1/session1"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected to WebSocket")

            # Start a listener task
            async def listener():
                try:
                    while True:
                        msg = await websocket.recv()
                        data = json.loads(msg)

                        # Print relevant parts
                        if "serverContent" in data:
                            model_turn = data["serverContent"].get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    if "text" in part:
                                        print(f"GEMINI TEXT: {part['text']}")
                                    if "inlineData" in part:
                                        print(
                                            f"GEMINI AUDIO: {len(part['inlineData']['data'])} bytes"
                                        )
                                    if "functionCall" in part:
                                        print(
                                            f"GEMINI TOOL CALL: {part['functionCall']['name']}({part['functionCall']['args']})"
                                        )

                        if "outputAudioTranscription" in data:
                            transcript = data["outputAudioTranscription"].get(
                                "finalTranscript"
                            )
                            if transcript:
                                print(f"GEMINI TRANSCRIPT: {transcript}")

                        if "type" in data and data["type"] == "match":
                            print(f"BACKEND MATCH: {data['count']}")

                except websockets.exceptions.ConnectionClosed:
                    print("Connection closed by server")

            listener_task = asyncio.create_task(listener())

            # Send handshakes
            await websocket.send(
                json.dumps({"type": "text", "text": "Neural handshake"})
            )
            print("Sent handshake 1")
            await asyncio.sleep(2)
            await websocket.send(
                json.dumps({"type": "text", "text": "Neural handshake"})
            )
            print("Sent handshake 2")

            # Wait for more responses
            await asyncio.sleep(10)

            # Close
            await websocket.close()
            await listener_task

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_ws())
