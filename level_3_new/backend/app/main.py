import asyncio
import base64
import json
import logging
import os
import warnings

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

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

# Cloud Run injects $PORT (8080 today, but it is documented as "whatever we
# tell you"). Reading it is the difference between honouring the contract and
# happening to agree with it.
PORT = int(os.getenv("PORT", "8080"))
APP_NAME = "alpha-drone"
FRONTEND_DIST = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../frontend/dist")
)

# Binary frame prefixes. These are the wire contract with
# frontend/src/useGeminiSocket.js. They are named here and shipped in the
# `config` frame below so the client reads them at runtime instead of keeping
# its own hardcoded copy that can drift silently.
AUDIO_PREFIX = 1
JPEG_PREFIX = 2

# Input audio rate the client is expected to send. The client confirms (or
# corrects) this with an `audio_config` message once it knows what rate the
# browser actually granted it -- see AudioRecorder in the frontend.
DEFAULT_INPUT_SAMPLE_RATE = 16000

# Response modality. This used to be inferred with
# `"live" in model_name.lower()`, which gave the right answer for
# gemini-3.1-flash-live-preview by coincidence -- it is a half-cascade model,
# not a native-audio one, and any future name without the substring would have
# silently flipped the whole session to TEXT and muted the demo.
RESPONSE_MODALITY = os.getenv("RESPONSE_MODALITY", "AUDIO").strip().upper()
if RESPONSE_MODALITY not in ("AUDIO", "TEXT"):
    logger.warning(f"Unknown RESPONSE_MODALITY={RESPONSE_MODALITY!r}; using AUDIO")
    RESPONSE_MODALITY = "AUDIO"

# Origin allowlist for the WebSocket handshake. CORS does not apply to
# WebSockets, so this is the only thing standing between a public Cloud Run URL
# and anyone streaming into your billed Live session. Unset means allow all,
# which is the right default for local work but is warned about loudly.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# The heartbeat enters the conversation as a real user turn (see heartbeat_task).
# Kept on by default because the current Live model is not proactive, but
# switchable without editing code.
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() not in (
    "0",
    "false",
    "no",
)


def send_text_stimulus(live_request_queue: LiveRequestQueue, text: str) -> None:
    """Send a text turn to the model.

    LiveRequestQueue.send_realtime() only accepts types.Blob; text has to go
    through send_content(). For Gemini 3.x Live, ADK routes a single-part text
    Content to send_realtime_input(text=...) internally.
    """
    live_request_queue.send_content(
        types.Content(role="user", parts=[types.Part(text=text)])
    )


# ========================================
# Phase 1: Application Initialization (once at startup)
# ========================================

app = FastAPI()

# CORS covers the HTTP routes only -- it has no bearing on the WebSocket
# handshake, which is guarded by check_origin() instead. allow_credentials is
# False deliberately: pairing it with allow_origins=["*"] is rejected by every
# browser, and nothing here sends credentials anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if ALLOWED_ORIGINS:
    logger.info(f"WebSocket origin allowlist: {', '.join(ALLOWED_ORIGINS)}")
else:
    logger.warning(
        "ALLOWED_ORIGINS is unset: the WebSocket accepts any origin. Fine for "
        "local work; set it before exposing this on a public URL."
    )


def check_origin(origin: str | None) -> bool:
    """Whether a handshake from this Origin may open a session.

    An empty allowlist means allow everything (local default). A non-browser
    client can omit Origin entirely, so a missing header is only accepted when
    the allowlist is empty.
    """
    if not ALLOWED_ORIGINS:
        return True
    return origin in ALLOWED_ORIGINS


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
    origin = websocket.headers.get("origin")
    if not check_origin(origin):
        logger.warning(f"Rejected WebSocket handshake from origin: {origin!r}")
        await websocket.close(code=1008, reason="origin not allowed")
        return

    await websocket.accept()
    logger.info(f"WebSocket connected: {user_id}/{session_id}")

    # Input rate the client is sending. Overridden by an `audio_config` message
    # if the browser refused the requested rate.
    input_sample_rate = DEFAULT_INPUT_SAMPLE_RATE

    # Send initial config to client so they know what FPS/Heartbeat to use, and
    # which binary prefixes to tag frames with.
    config_msg = {
        "type": "config",
        "video_fps": VIDEO_FPS,
        "frame_interval_ms": FRAME_INTERVAL_MS,
        "heartbeat_interval": HEARTBEAT_INTERVAL,
        "audio_prefix": AUDIO_PREFIX,
        "jpeg_prefix": JPEG_PREFIX,
        "input_sample_rate": DEFAULT_INPUT_SAMPLE_RATE,
    }
    await websocket.send_text(json.dumps(config_msg))

    # NOTE: there used to be a canned audio greeting here -- mock/mock_audio.pcm
    # read off disk and sent wrapped in a synthetic serverContent.modelTurn, i.e.
    # a recording indistinguishable from a real model turn. It also only existed
    # locally, since the Dockerfile never copies mock/ into the image.
    # The model's opening line now comes from the model: the "Neural handshake"
    # stimulus below forces the first turn, and the agent instruction ends with
    # 'Say "Scanner Online." to initialize.'

    # ========================================
    # Phase 2: Session Initialization (once per streaming session)
    # ========================================

    # Response modality is a deployment decision (RESPONSE_MODALITY), not
    # something to infer from the model name. Transcription only applies to the
    # audio path.
    model_name = root_agent.model
    response_modalities = [RESPONSE_MODALITY]
    wants_audio = RESPONSE_MODALITY == "AUDIO"

    run_config = RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=response_modalities,
        input_audio_transcription=types.AudioTranscriptionConfig()
        if wants_audio
        else None,
        output_audio_transcription=types.AudioTranscriptionConfig()
        if wants_audio
        else None,
        # The server will emit resumption handles; nothing captures or replays
        # them yet, so a reconnect is always a cold session. Left enabled
        # because it is free, but do not read it as working resumption.
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
    send_text_stimulus(live_request_queue, "Neural handshake")

    async def upstream_task() -> None:
        """Receives messages from WebSocket and sends to LiveRequestQueue."""
        frame_count = 0
        audio_count = 0
        nonlocal last_input_time, input_sample_rate

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

                    if msg_type == AUDIO_PREFIX:  # PCM, rate reported by client
                        audio_count += 1
                        if audio_count % 50 == 0:
                            logger.info(f"Received audio packet #{audio_count}")
                        try:
                            audio_blob = types.Blob(
                                mime_type=f"audio/pcm;rate={input_sample_rate}",
                                data=payload,
                            )
                            live_request_queue.send_realtime(audio_blob)
                        except Exception as e:
                            logger.error(f"Failed to send audio blob: {e}")

                    elif msg_type == JPEG_PREFIX:  # VIDEO (JPEG)
                        frame_count += 1
                        if frame_count % 10 == 0:
                            logger.info(f"Received binary image frame #{frame_count}")
                        try:
                            image_blob = types.Blob(
                                mime_type="image/jpeg", data=payload
                            )
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

                    # The browser may refuse the 16 kHz AudioContext we asked
                    # for and hand back the hardware rate instead. Labelling
                    # 48 kHz samples as 16 kHz makes speech unintelligible to
                    # the model with no error anywhere, so the client tells us
                    # the rate it actually got.
                    if json_message.get("type") == "audio_config":
                        reported = json_message.get("sample_rate")
                        if isinstance(reported, int | float) and reported > 0:
                            input_sample_rate = int(reported)
                            if input_sample_rate != DEFAULT_INPUT_SAMPLE_RATE:
                                logger.warning(
                                    f"Client is sending {input_sample_rate} Hz audio, "
                                    f"not {DEFAULT_INPUT_SAMPLE_RATE} Hz; tagging blobs accordingly"
                                )
                            else:
                                logger.info(
                                    f"Client audio rate: {input_sample_rate} Hz"
                                )
                        continue

                    # Extract text from JSON and send to LiveRequestQueue
                    if json_message.get("type") == "text":
                        user_text = json_message.get("text", "")
                        logger.info(f"USER TEXT: {user_text}")
                        send_text_stimulus(live_request_queue, user_text)

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
                            mime_type=f"audio/pcm;rate={input_sample_rate}",
                            data=audio_data,
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
        """Sends periodic 'Keep scanning' stimulus to keep model active.

        Note this is not a transport keepalive -- it enters the conversation as
        a real user turn, which the model may answer out loud. It only fires
        when the client has gone quiet, since `last_input_time` is refreshed by
        every upstream frame. Set HEARTBEAT_ENABLED=false to turn it off.
        """
        if not HEARTBEAT_ENABLED:
            logger.info("Heartbeat disabled")
            return
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)  # Heartbeat every X seconds
                now = asyncio.get_event_loop().time()
                if now - last_input_time > (HEARTBEAT_INTERVAL - 2.0):
                    logger.debug("Sending heartbeat stimulus to Gemini...")
                    send_text_stimulus(live_request_queue, "CONTINUE_SURVEILLANCE")
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
                    # `return`, not `break`: break only leaves the `for fc`
                    # loop, so the `async for` below carried on to
                    # send_text(event_json) on a socket we had just closed and
                    # logged 'Cannot call "send" once a close message has been
                    # sent.' on every trigger. The session is over here.
                    return
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

    # Two tasks define the session's lifetime: the client half (upstream) and
    # the model half (downstream). Whichever ends first ends the session.
    # The heartbeat is a helper, never a trigger -- it is cancelled with
    # everything else but its finishing must not tear the session down (with
    # HEARTBEAT_ENABLED=false it returns immediately).
    #
    # This used to be `asyncio.gather(upstream, downstream, heartbeat)`, which
    # was wrong twice over: heartbeat_task loops forever, so a clean client
    # disconnect never completed the gather -- meaning the `finally` below never
    # ran and the billed Gemini Live session stayed open until something else
    # happened to raise. And gather propagates the first exception while leaving
    # its siblings running, so every session also leaked an orphaned heartbeat.
    lifecycle = [
        asyncio.create_task(upstream_task(), name="upstream"),
        asyncio.create_task(downstream_task(), name="downstream"),
    ]
    helpers = [asyncio.create_task(heartbeat_task(), name="heartbeat")]

    try:
        done, _pending = await asyncio.wait(
            lifecycle, return_when=asyncio.FIRST_COMPLETED
        )
        # Surface a real failure instead of letting it die with the task.
        for task in done:
            task.result()
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=False)  # Reduced stack trace noise
    finally:
        # ========================================
        # Phase 4: Session Termination
        # ========================================

        # Close the queue FIRST. It is synchronous, it is what actually ends the
        # billed Live session, and it must not sit behind an `await` -- once we
        # suspend there is no guarantee the ASGI layer schedules this coroutine
        # again. Closing it also lets run_live() finish on its own, so the
        # cancellation below usually has nothing left to do.
        logger.debug("Closing live_request_queue")
        live_request_queue.close()

        for task in lifecycle + helpers:
            task.cancel()
        await asyncio.gather(*lifecycle, *helpers, return_exceptions=True)


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
