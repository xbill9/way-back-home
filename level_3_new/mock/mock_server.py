import asyncio
import base64
import json
import os
import traceback

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

app = FastAPI()

PORT = 8080
# Use absolute paths relative to this file's directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_FILE = os.path.join(BASE_DIR, "mock_audio.pcm")
FRONTEND_DIST = os.path.abspath(os.path.join(BASE_DIR, "../frontend/dist"))

# Must match backend/app/main.py.
AUDIO_PREFIX = 1
JPEG_PREFIX = 2


# WebSocket Endpoint
@app.websocket("/ws/user1/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    print(f"Client connected (Session: {session_id})")
    try:
        # Identify as mock server so frontend can show a banner
        await websocket.send_text(json.dumps({"mock": True}))
        print("Sent mock server identification")

        # Same config frame the real backend leads with, including the binary
        # prefixes the client adopts at runtime.
        await websocket.send_text(
            json.dumps(
                {
                    "type": "config",
                    "video_fps": 2.0,
                    "frame_interval_ms": 500,
                    "heartbeat_interval": 10.0,
                    "audio_prefix": AUDIO_PREFIX,
                    "jpeg_prefix": JPEG_PREFIX,
                    "input_sample_rate": 16000,
                }
            )
        )

        # Send initial audio greeting immediately
        if os.path.exists(AUDIO_FILE):
            print(f"Sending initial audio greeting from {AUDIO_FILE}...")
            with open(AUDIO_FILE, "rb") as f:
                audio_content = f.read()

            b64_audio = base64.b64encode(audio_content).decode("utf-8")

            response = {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/pcm;rate=24000",
                                    "data": b64_audio,
                                }
                            }
                        ]
                    }
                }
            }
            await websocket.send_text(json.dumps(response))
            print("Sent mock audio response")

            # Send mock tool call shortly after
            print("Sending mock tool call in 2 seconds...")
            await asyncio.sleep(2)
            tool_call_response = {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "report_digit",
                                    "args": {
                                        "digit": 3  # Changed to digit for standard compliance
                                    },
                                }
                            }
                        ]
                    }
                }
            }
            await websocket.send_text(json.dumps(tool_call_response))

            # The real backend translates report_digit into a `match` frame and
            # sends the raw event too; the client acts on `match` only. Mirror
            # both so the mock exercises the same contract.
            await websocket.send_text(
                json.dumps({"type": "match", "count": 3, "digit": 3})
            )
            print("Sent mock tool call + match signal (report_digit: 3)")
        else:
            print(f"Error: {AUDIO_FILE} not found")

        audio_count = 0
        frame_count = 0

        while True:
            # Continue to listen for messages to keep connection open and log
            # them. This must be receive(), not receive_text(): the frontend
            # streams *only* binary frames, and receive_text() reads
            # message["text"], so the first audio packet raised
            # "Error in websocket loop: 'text'" (a bare KeyError) and killed
            # the loop the moment streaming started.
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                print("Client requested disconnect")
                break

            if "bytes" in message:
                # Same 1-byte prefix the real backend decodes: 1 = audio
                # (16kHz PCM), 2 = video (JPEG).
                binary_data = message["bytes"]
                if len(binary_data) < 2:
                    continue

                msg_type = binary_data[0]
                payload_size = len(binary_data) - 1

                if msg_type == AUDIO_PREFIX:
                    audio_count += 1
                    if audio_count % 50 == 0:
                        print(
                            f"Received audio packet #{audio_count} ({payload_size} bytes)"
                        )
                elif msg_type == JPEG_PREFIX:
                    frame_count += 1
                    if frame_count % 10 == 0:
                        print(
                            f"Received image frame #{frame_count} ({payload_size} bytes)"
                        )
                else:
                    print(f"Received unknown binary prefix: {msg_type}")
                continue

            text = message.get("text")
            if text is None:
                continue

            try:
                msg_data = json.loads(text)
                print(f"Received message type: {msg_data.get('type') or 'unknown'}")
            except json.JSONDecodeError:
                print(f"Received non-JSON message: {text[:100]}...")

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        # repr(), not str(): a bare KeyError prints as just 'text', which gave
        # no clue that it came from receive_text() hitting a binary frame.
        print(f"Error in websocket loop: {e!r}")
        traceback.print_exc()


# Serve Static Files (Fallback for SPA)
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")
    print(f"Serving static files from: {FRONTEND_DIST}")
else:
    print(f"Warning: Frontend build not found at {FRONTEND_DIST}")
    print("Please run 'npm run build' in the frontend directory.")

if __name__ == "__main__":
    # Run uvicorn programmatically
    uvicorn.run(app, host="0.0.0.0", port=PORT)
