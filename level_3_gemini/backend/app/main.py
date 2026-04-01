import asyncio
import json
import logging
import uvicorn
import warnings
import os
import base64


from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Patch ADK for Gemini 3.1 Live API compatibility
import patch_adk

patch_adk.apply_patches()

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Default to INFO
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Suppress noisy loggers
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("google_adk").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Load environment variables from .env file BEFORE importing agent
load_dotenv()

# Pre-flight check: Verify GOOGLE_API_KEY exists
if not os.getenv("GOOGLE_API_KEY"):
    logger.critical("FATAL ERROR: GOOGLE_API_KEY not found in environment variables.")
    logger.critical("Please set it in your .env file or export it to your shell.")
    # Exit if running as a script, otherwise raise error
    if __name__ == "__main__":
        import sys

        sys.exit(1)

# Configuration from environment variables
# Range validation: 0.5 to 5.0 FPS, 5.0 to 30.0s Heartbeat
VIDEO_FPS = max(0.5, min(float(os.getenv("VIDEO_FPS", "2.0")), 5.0))
HEARTBEAT_INTERVAL = max(5.0, min(float(os.getenv("HEARTBEAT_INTERVAL", "10.0")), 30.0))
FRAME_INTERVAL_MS = int(1000 / VIDEO_FPS)

# Log the active configuration
logger.info(f"System Config: {VIDEO_FPS} FPS, {HEARTBEAT_INTERVAL}s Heartbeat")

# Import agent after loading environment variables
# pylint: disable=wrong-import-position
from biometric_agent.agent import root_agent  # noqa: E402

# Suppress Pydantic serialization warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

PORT = 8080
APP_NAME = "alpha-drone"
FRONTEND_DIST = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../frontend/dist")
)
# ========================================
# Phase 1: Application Initialization (once at startup)
# ========================================

app = FastAPI()

# Add CORS middleware to allow WebSocket connections from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Define your session service
session_service = InMemorySessionService()

# Define your runner
runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

# ========================================
# WebSocket Endpoint
# ========================================


@app.websocket("/ws/{user_id}/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str,
    session_id: str,
    proactivity: bool = True,
    affective_dialog: bool = False,
) -> None:
    """WebSocket endpoint for bidirectional streaming with ADK.

    Args:
        websocket: The WebSocket connection
        user_id: User identifier
        session_id: Session identifier
        proactivity: Enable proactive audio (native audio models only)
        affective_dialog: Enable affective dialog (native audio models only)
    """
    await websocket.accept()
    logger.info(f"WebSocket connected: {user_id}/{session_id}")

    # Send initial config to client so they know what FPS/Heartbeat to use
    config_msg = {
        "type": "config",
        "video_fps": VIDEO_FPS,
        "frame_interval_ms": FRAME_INTERVAL_MS,
        "heartbeat_interval": HEARTBEAT_INTERVAL,
    }
    await websocket.send_text(json.dumps(config_msg))

    # Send initial audio greeting if it exists
    # This ensures the user hears the "startup audio" immediately,
    # as Gemini 3.1 Flash Live is not yet proactive.
    mock_audio_path = os.path.join(
        os.path.dirname(__file__), "../../mock/mock_audio.pcm"
    )
    if os.path.exists(mock_audio_path):
        logger.info(f"Sending initial audio greeting from {mock_audio_path}...")
        try:
            with open(mock_audio_path, "rb") as f:
                audio_content = f.read()
            b64_audio = base64.b64encode(audio_content).decode("utf-8")
            greeting_msg = {
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
            await websocket.send_text(json.dumps(greeting_msg))
        except Exception as e:
            logger.error(f"Failed to send initial audio greeting: {e}")

    # ========================================
    # Phase 2: Session Initialization (once per streaming session)
    # ========================================

    # Automatically determine response modality based on model architecture
    # Native audio models (containing "native-audio" in name)
    # ONLY support AUDIO response modality.
    # Half-cascade models support both TEXT and AUDIO;
    # we default to TEXT for better performance.

    model_name = root_agent.model
    is_native_audio = (
        "native-audio" in model_name.lower() or "live" in model_name.lower()
    )

    if is_native_audio:
        # Native audio models require AUDIO response modality
        # with audio transcription
        response_modalities = ["AUDIO"]

        # Build RunConfig
        # Note: Proactivity and affective dialog are not supported in Gemini 3.1 Flash Live
        run_config = RunConfig(
            streaming_mode=StreamingMode.BIDI,
            response_modalities=response_modalities,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            session_resumption=types.SessionResumptionConfig(),
        )
        logger.info(f"Model Config: {model_name} (Modalities: {response_modalities})")
    else:
        # Half-cascade models support TEXT response modality
        # for faster performance
        response_modalities = ["TEXT"]
        run_config = RunConfig(
            streaming_mode=StreamingMode.BIDI,
            response_modalities=response_modalities,
            input_audio_transcription=None,
            output_audio_transcription=None,
            session_resumption=types.SessionResumptionConfig(),
        )
        logger.info(f"Model Config: {model_name} (Modalities: {response_modalities})")

    # Get or create session (handles both new sessions and reconnections)
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if not session:
        await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

    # ========================================
    # Phase 3: Active Session (concurrent bidirectional communication)
    # ========================================

    live_request_queue = LiveRequestQueue()

    # Send an initial "Neural handshake" to the model to wake it up/force a turn
    logger.info("Sending initial 'Neural handshake' stimulus to model...")
    live_request_queue.send_realtime("Neural handshake")

    async def upstream_task() -> None:
        """Receives messages from WebSocket and sends to LiveRequestQueue."""
        frame_count = 0
        audio_count = 0
        nonlocal last_input_time

        try:
            while True:
                # Receive message from WebSocket (text or binary)
                message = await websocket.receive()
                last_input_time = asyncio.get_event_loop().time()
                if message.get("type") == "websocket.disconnect":
                    logger.info("Client requested disconnect")
                    break

                # Handle binary frames (audio or video data)
                if "bytes" in message:
                    binary_data = message["bytes"]
                    if len(binary_data) < 1:
                        continue

                    msg_type = binary_data[0]
                    payload = binary_data[1:]

                    if not payload:
                        continue

                    if msg_type == 1:  # AUDIO (16kHz PCM)
                        audio_count += 1
                        if audio_count % 50 == 0:
                            logger.info(f"Received audio packet #{audio_count}")
                        try:
                            audio_blob = types.Blob(
                                mime_type="audio/pcm;rate=16000", data=payload
                            )
                            live_request_queue.send_realtime(audio_blob)
                        except Exception as e:
                            logger.error(f"Failed to send audio blob: {e}")

                    elif msg_type == 2:  # VIDEO (JPEG)
                        frame_count += 1
                        if frame_count % 10 == 0:
                            logger.info(f"Received binary image frame #{frame_count}")
                        try:
                            image_blob = types.Blob(mime_type="image/jpeg", data=payload)
                            live_request_queue.send_realtime(image_blob)
                        except Exception as e:
                            logger.error(f"Failed to send image blob: {e}")

                # Handle text frames (JSON messages)
                elif "text" in message:
                    text_data = message["text"]
                    try:
                        json_message = json.loads(text_data)
                    except json.JSONDecodeError:
                        logger.warning(f"Received invalid JSON: {text_data[:100]}...")
                        continue

                    # Extract text from JSON and send to LiveRequestQueue
                    if json_message.get("type") == "text":
                        user_text = json_message.get("text", "")
                        logger.info(f"USER TEXT: {user_text}")
                        live_request_queue.send_realtime(user_text)

                    # Handle audio data (microphone)
                    elif json_message.get("type") == "audio":
                        # Decode base64 audio data
                        audio_b64 = json_message.get("data", "")
                        if not audio_b64:
                            continue
                        audio_data = base64.b64decode(audio_b64)

                        audio_count += 1
                        logger.info(
                            f"Received audio packet #{audio_count} (size: {len(audio_data)} bytes)"
                        )

                        # Send to Live API as PCM 16kHz
                        audio_blob = types.Blob(
                            mime_type="audio/pcm;rate=16000", data=audio_data
                        )
                        live_request_queue.send_realtime(audio_blob)

                    # Handle image data
                    elif json_message.get("type") == "image":
                        # Decode base64 image data
                        image_b64 = json_message.get("data", "")
                        if not image_b64:
                            continue
                        image_data = base64.b64decode(image_b64)
                        mime_type = json_message.get("mimeType", "image/jpeg")

                        frame_count += 1
                        logger.info(
                            f"Received image frame #{frame_count} (size: {len(image_data)} bytes)"
                        )

                        # Send image as blob
                        image_blob = types.Blob(mime_type=mime_type, data=image_data)
                        live_request_queue.send_realtime(image_blob)
        except Exception as e:
            logger.error(f"Error in upstream_task: {e}")
        finally:
            logger.debug("upstream_task terminating")

    # Track last match for deduplication
    last_match_digit = None
    last_match_time = 0
    last_input_time = asyncio.get_event_loop().time()

    def extract_function_calls(event):
        """Helper to extract function calls from various event structures."""
        calls = []
        # 1. Standard ADK
        if hasattr(event, "tool_call") and event.tool_call:
            calls.extend(event.tool_call.function_calls)
        # 2. Gemini Live API server_content
        if hasattr(event, "server_content") and event.server_content:
            if (
                hasattr(event.server_content, "model_turn")
                and event.server_content.model_turn
            ):
                for part in event.server_content.model_turn.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        calls.append(part.function_call)
        # 3. Direct content fallback
        if hasattr(event, "content") and event.content:
            for part in event.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    calls.append(part.function_call)
        return calls

    async def heartbeat_task() -> None:
        """Sends periodic 'Keep scanning' stimulus to keep model active."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)  # Heartbeat every X seconds
                now = asyncio.get_event_loop().time()
                if now - last_input_time > (HEARTBEAT_INTERVAL - 2.0):
                    logger.debug("Sending heartbeat stimulus to Gemini...")
                    live_request_queue.send_realtime("CONTINUE_SURVEILLANCE")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

    async def downstream_task() -> None:
        """Receives Events from run_live() and sends to WebSocket."""
        logger.info("Connecting to Gemini Live API...")
        model_audio_count = 0
        nonlocal last_match_digit, last_match_time

        async for event in runner.run_live(
            user_id=user_id,
            session_id=session_id,
            live_request_queue=live_request_queue,
            run_config=run_config,
        ):
            # Use centralized extraction
            function_calls = extract_function_calls(event)

            # Extract Function Responses for logging
            function_responses = []
            if hasattr(event, "server_content") and event.server_content:
                if (
                    hasattr(event.server_content, "model_turn")
                    and event.server_content.model_turn
                ):
                    for part in event.server_content.model_turn.parts:
                        if (
                            hasattr(part, "function_response")
                            and part.function_response
                        ):
                            function_responses.append(part.function_response)

            # Process Function Calls
            for fc in function_calls:
                logger.info(f"[FUNCTION CALL] {fc.name}({fc.args})")
                if fc.name == "report_digit":
                    count = fc.args.get("count") or fc.args.get("digit")
                    if count is not None:
                        current_time = asyncio.get_event_loop().time()
                        # Improved deduplication: 1.5s window OR different digit
                        if (
                            count != last_match_digit
                            or (current_time - last_match_time) >= 1.5
                        ):
                            last_match_digit = count
                            last_match_time = current_time
                            match_msg = {
                                "type": "match",
                                "count": count,
                                "digit": count,
                            }
                            logger.info(f"Sending MATCH signal to frontend: {count}")
                            await websocket.send_text(json.dumps(match_msg))
                elif fc.name == "trigger_system_error":
                    logger.warning("SYSTEM ERROR TRIGGERED BY MODEL")
                    error_msg = {
                        "type": "system_error",
                        "message": "CRITICAL PROTOCOL VIOLATION: OFFENSIVE GESTURE DETECTED. NEURAL LINK SEVERED.",
                    }
                    await websocket.send_text(json.dumps(error_msg))
                    await websocket.close()
                    break
                elif fc.name == "trigger_heavy_metal_mode":
                    logger.info("HEAVY METAL MODE ACTIVATED")
                    hm_msg = {
                        "type": "heavy_metal",
                        "message": "ROCK ON! HEAVY METAL OVERRIDE DETECTED.",
                    }
                    await websocket.send_text(json.dumps(hm_msg))

            # ... rest of the transcript and audio handling ...

            # Process Function Responses
            for fr in function_responses:
                logger.info(f"[FUNCTION RESPONSE] {fr.name} -> {fr.response}")

            # Check for user input transcription (Text or Audio Transcript)
            input_transcription = getattr(event, "input_audio_transcription", None)
            if input_transcription and input_transcription.final_transcript:
                logger.info(f"USER TRANSCRIPT: {input_transcription.final_transcript}")

            # Check for model output transcription
            output_transcription = getattr(event, "output_audio_transcription", None)
            if output_transcription and output_transcription.final_transcript:
                logger.info(
                    f"GEMINI TRANSCRIPT: {output_transcription.final_transcript}"
                )

            # Check for model turn content (text or audio)
            if hasattr(event, "server_content") and event.server_content:
                if (
                    hasattr(event.server_content, "model_turn")
                    and event.server_content.model_turn
                ):
                    for part in event.server_content.model_turn.parts:
                        if part.text:
                            logger.info(f"GEMINI TEXT: {part.text}")
                        if part.inline_data:
                            model_audio_count += 1
                            if model_audio_count % 50 == 0:
                                logger.info(
                                    f"Sent model audio chunk #{model_audio_count} to client"
                                )

            event_json = event.model_dump_json(exclude_none=True, by_alias=True)
            await websocket.send_text(event_json)
        logger.info("Gemini Live API connection closed.")

    # Run all tasks concurrently
    # Exceptions from either task will propagate and cancel the other tasks
    try:
        await asyncio.gather(upstream_task(), downstream_task(), heartbeat_task())
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=False)  # Reduced stack trace noise
    finally:
        # ========================================
        # Phase 4: Session Termination
        # ========================================

        # Always close the queue, even if exceptions occurred
        logger.debug("Closing live_request_queue")
        live_request_queue.close()


# Serve Static Files (Fallback for SPA)
# Mount static files if directory exists
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")
    print(f"Serving static files from: {FRONTEND_DIST}")
else:
    print(f"Warning: Frontend build not found at {FRONTEND_DIST}")
    print("Please run 'npm run build' in the frontend directory.")

if __name__ == "__main__":
    # Run uvicorn programmatically
    uvicorn.run(app, host="0.0.0.0", port=PORT)
