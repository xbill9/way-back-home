import asyncio
import json
import logging
import os
import time
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
#
# The Live API capabilities guide says video frames go in "as individual images
# (e.g., JPEG or PNG) at a specific frame rate (max 1 frame per second)", and
# surplus frames are billed and burn the audio+video session budget faster. So
# the default is 1.0, the documented maximum.
#
# 1.0 is NOT a hard ceiling, which it briefly was. A knob that silently ignores
# you is worse than no knob: VIDEO_FPS=2 clamping back to 1.0 with no warning
# meant the obvious first thing to try when detection feels bad did nothing, and
# looked like it had.
#
# Raising it does not buy accuracy, which was measured rather than assumed.
# `scripts/scan_accuracy.py` on 2026-08-13, 60% of frames blurred, jitter on:
# 1.0 scored 10/10 with a 0.68s median, 2.0 scored 10/10 with 1.80s. More frames
# were slightly slower to answer and no more accurate. Raise it only with a
# measurement in hand.
VIDEO_FPS = max(0.5, min(float(os.getenv("VIDEO_FPS", "1.0")), 5.0))
HEARTBEAT_INTERVAL = max(5.0, min(float(os.getenv("HEARTBEAT_INTERVAL", "10.0")), 30.0))
FRAME_INTERVAL_MS = int(1000 / VIDEO_FPS)

# Capture size and JPEG quality, shipped to the client in the config frame for
# the same reason the frame prefixes are: one definition, on the server, tunable
# without rebuilding the frontend.
#
# Measured with scripts/scan_accuracy.py, five trials each, all 5/5 correct:
#
#     640x480 q60   128.6 kbit/s   p50 0.76s   <- the original
#     640x480 q40   118.0 kbit/s   p50 1.72s
#     480x360 q50    77.3 kbit/s   p50 0.64s   <- default
#     320x240 q50    44.5 kbit/s   p50 0.67s
#     320x240 q40    40.1 kbit/s   p50 0.52s
#
# Quality is nearly free to lower and buys nothing; pixels are the whole cost.
#
# And yet the default is 640x480, the most expensive row. 480x360 shipped first
# on the strength of that table and made real-world accuracy visibly worse. The
# table is not wrong, it is unrepresentative: every fixture has a hand filling
# the frame, so shrinking it costs nothing there, while a hand at arm's length
# from a laptop occupies a fraction of the frame and loses the fingers first.
#
# Do not trade video resolution for bandwidth. The uplink is ~77% microphone
# (256 kbit/s of raw PCM that cannot be compressed), so the savings are in the
# audio gate, not here -- and accuracy is the one thing video is buying.
VIDEO_WIDTH = max(160, min(int(os.getenv("VIDEO_WIDTH", "640")), 1920))
VIDEO_HEIGHT = max(120, min(int(os.getenv("VIDEO_HEIGHT", "480")), 1080))
JPEG_QUALITY = max(20, min(int(os.getenv("JPEG_QUALITY", "60")), 95))

# Pin the voice. With speech_config unset the API picks one per session, so the
# scanner sounded like a different character on every run -- which reads as a
# bug in a demo built around one machine talking to you. Charon is the deepest
# of the prebuilt voices, which suits a cold surveillance system; VOICE_NAME
# overrides it (Puck, Kore, Fenrir, Aoede are the others).
VOICE_NAME = os.getenv("VOICE_NAME", "Charon").strip() or "Charon"

# Write every received JPEG to disk. Unset means off, which is the default:
# these are frames of somebody's face and room, so it is opt-in and never
# implicit.
#
# It exists because every accuracy investigation in this project has been run
# against fixture images -- five clean, deliberate, hand-fills-frame poses --
# while the misreads happen on a real camera. Twice now a reported misread has
# failed to reproduce against the fixtures, which means the fixtures are not the
# thing to be looking at. This is how to see what the model was actually sent.
DEBUG_FRAME_DIR = os.getenv("DEBUG_FRAME_DIR", "").strip() or None

# On which `report_digit` call of a single scan the backend asks for the spoken
# confirmation out loud.
#
# A rescue, not a protocol. On this model a scan draws exactly one call and the
# model speaks on its own, so this should never fire; it is here because the
# failure it catches is invisible from the outside. A repeat run keeps answering
# correctly and silently -- the digit reaches the UI in under a second either
# way -- so it presents as "the scanner has gone quiet", with nothing in any log
# saying why. Now it says why, and does the one thing measured to break the run:
# put a new user turn in front of the model. (In the walkie-talkie tree, a
# frustrated second "scan" ended a 13-call run instantly, on the next call.)
#
# Clamped at 2, not 1: the tool result here is BLOCKING, so it prompts a turn of
# its own and the confirmation arrives without help. Nudging on the first call
# would be an extra turn on every single scan.
STORM_NUDGE_AFTER = max(2, min(int(os.getenv("STORM_NUDGE_AFTER", "3")), 20))

# Languages the scanner can be asked to speak, chosen per session from the UI.
#
# This is the cheapest possible proof that the model is really being called:
# a recording cannot answer in Spanish. The demo otherwise looks the same
# whether it is driving the Live API or replaying a file -- which, before the
# canned greeting was removed, it partly was.
#
# The code is BCP-47 for speech_config; the name is what the model is told to
# speak, since it translates the phrases itself rather than us shipping
# translations we cannot check.
LANGUAGES = {
    "en-US": "English",
    "es-ES": "Spanish",
    "fr-FR": "French",
    "de-DE": "German",
    "it-IT": "Italian",
    "pt-BR": "Brazilian Portuguese",
    "hi-IN": "Hindi",
    "ja-JP": "Japanese",
    "ko-KR": "Korean",
}
DEFAULT_LANGUAGE = os.getenv("LANGUAGE_CODE", "en-US").strip()
if DEFAULT_LANGUAGE not in LANGUAGES:
    logger.warning(f"Unknown LANGUAGE_CODE={DEFAULT_LANGUAGE!r}; using en-US")
    DEFAULT_LANGUAGE = "en-US"

# Log the active configuration
logger.info(f"System Config: {VIDEO_FPS} FPS, {HEARTBEAT_INTERVAL}s Heartbeat")

# Import agent after loading environment variables
# pylint: disable=wrong-import-position
from audio_codec import pcm16_to_ulaw  # noqa: E402
from biometric_agent.agent import MODEL_ID, root_agent  # noqa: E402

# Suppress Pydantic serialization warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

# Cloud Run injects $PORT (8080 today, but it is documented as "whatever we
# tell you"). Reading it is the difference between honouring the contract and
# happening to agree with it.
PORT = int(os.getenv("PORT", "8080"))
APP_NAME = "alpha-drone"
# One directory per process, so runs do not interleave on disk.
_RUN_STAMP = time.strftime("%Y%m%d-%H%M%S")
FRONTEND_DIST = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../frontend/dist")
)

# Binary frame prefixes. These are the wire contract with
# frontend/src/useGeminiSocket.js. They are named here and shipped in the
# `config` frame below so the client reads them at runtime instead of keeping
# its own hardcoded copy that can drift silently.
AUDIO_PREFIX = 1
JPEG_PREFIX = 2
# Model audio, going the other way. It used to ride the event JSON as base64,
# which inflates binary by a third: over a 14s session the downlink was 220KB,
# of which 202KB was base64 audio. As mu-law on its own binary frame the same
# audio is a quarter of that.
MODEL_AUDIO_PREFIX = 3

# pcm16 | mulaw | base64. base64 is the original path -- audio inside the event
# JSON -- and exists as a one-variable way back if the binary path ever
# misbehaves in a browser. mulaw halves pcm16 again and is lossy; see
# audio_codec.py.
MODEL_AUDIO_ENCODING = os.getenv("MODEL_AUDIO_ENCODING", "mulaw").strip().lower()
if MODEL_AUDIO_ENCODING not in ("pcm16", "mulaw", "base64"):
    logger.warning(
        f"Unknown MODEL_AUDIO_ENCODING={MODEL_AUDIO_ENCODING!r}; using mulaw"
    )
    MODEL_AUDIO_ENCODING = "mulaw"

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
if RESPONSE_MODALITY == "TEXT":
    # Measured, not assumed: the model refuses TEXT at setup and closes the
    # socket with 1007, "The requested combination of response modalities
    # (TEXT) is not supported by the model." Every session dies immediately,
    # which is a poor way to discover that a documented knob is not real. Read
    # the output transcript instead -- it is enabled below and carries the same
    # words the audio does.
    logger.error(
        "RESPONSE_MODALITY=TEXT is not supported by the Live model and closes "
        "every session with 1007; forcing AUDIO. Read the output transcript."
    )
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


def _save_debug_frame(payload: bytes, index: int) -> None:
    """Persist one received frame, best effort.

    Never allowed to disturb the session: a full disk or a bad path is a
    debugging inconvenience, not a reason to drop a live call.
    """
    try:
        directory = os.path.join(DEBUG_FRAME_DIR, _RUN_STAMP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"frame_{index:05d}.jpg"), "wb") as handle:
            handle.write(payload)
    except Exception as exc:  # pragma: no cover - diagnostics only
        logger.debug(f"frame capture failed: {exc}")


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

# Define your runner. auto_create_session lets run_live() create the session on
# first use; without it a missing session is a ValueError and every caller has
# to hand-roll get-then-create. Defaults to False in ADK.
runner = Runner(
    app_name=APP_NAME,
    agent=root_agent,
    session_service=session_service,
    auto_create_session=True,
)

# ========================================
# WebSocket Endpoint
# ========================================


@app.websocket("/ws/{user_id}/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str,
    session_id: str,
) -> None:
    """WebSocket endpoint for bidirectional streaming with ADK.

    Args:
        websocket: The WebSocket connection
        user_id: User identifier
        session_id: Session identifier

    The language comes in as a `?lang=` query parameter rather than a message,
    because it has to be known before the Live session is opened: speech_config
    is part of the connect config and cannot be changed mid-session.
    """
    requested = websocket.query_params.get("lang", DEFAULT_LANGUAGE)
    language_code = requested if requested in LANGUAGES else DEFAULT_LANGUAGE
    if requested != language_code:
        logger.warning(f"Unsupported lang={requested!r}; using {language_code}")
    language_name = LANGUAGES[language_code]

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
        "model_audio_prefix": MODEL_AUDIO_PREFIX,
        "model_audio_encoding": MODEL_AUDIO_ENCODING,
        "model_audio_rate": 24000,
        "language_code": language_code,
        "language_name": language_name,
        "languages": LANGUAGES,
        "input_sample_rate": DEFAULT_INPUT_SAMPLE_RATE,
        "video_width": VIDEO_WIDTH,
        "video_height": VIDEO_HEIGHT,
        "jpeg_quality": JPEG_QUALITY,
        # The UI titles itself with this. Shipped rather than hardcoded in the
        # client for the same reason the frame prefixes are: one definition, on
        # the server, so a model change cannot leave the screen claiming the
        # wrong one.
        "model": MODEL_ID,
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
    model_name = MODEL_ID
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
        # Without this an audio+video session is capped at 2 minutes (audio-only
        # gets 15). This streams both continuously, so the cap is reachable in a
        # single demo run; compression "extends sessions to an unlimited amount
        # of time". Defaults to None in ADK, so it has to be asked for.
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        # Same voice every session. Unset, the API picks one per connection.
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)
            ),
            language_code=language_code,
        )
        if wants_audio
        else None,
        # Server-side VAD is left on API defaults, and that is a measured
        # decision rather than an omission.
        #
        # Speech in the room scores 0/5 with total silence (scripts/scan_accuracy
        # .py --noise chatter): continuous sound reads as a user turn that never
        # ends, so the model never gets one of its own. The obvious lever is
        # AutomaticActivityDetection(start_of_speech_sensitivity=LOW,
        # end_of_speech_sensitivity=HIGH) -- treat sound as speech less readily.
        # Measured on 2026-08-13, that took 0/5 to 1/5. Four of five prompts
        # still went unanswered, and LOW start sensitivity carries a real cost
        # for a softly-spoken user, so it is not worth carrying for that.
        #
        # The fix that works is upstream: do not send room noise at all. See the
        # gate in frontend/public/audio-processor.js.
    )
    logger.info(f"Model Config: {model_name} (Modalities: {response_modalities})")

    # ========================================
    # Phase 3: Active Session (concurrent bidirectional communication)
    # ========================================

    live_request_queue = LiveRequestQueue()

    # How many `report_digit` calls have arrived since the last turn we put to
    # the model -- i.e. for the scan currently being answered. A dict because
    # both nested tasks touch it: upstream resets it, downstream counts on it.
    scan_gate = {"calls": 0, "nudged": False}

    def put_user_turn(text: str) -> None:
        """Send a turn to the model, and start a new scan's call count.

        Every turn we put is a new question, so the count of calls answering the
        previous one stops here. The nudge in downstream_task deliberately does
        NOT go through this -- it is part of the scan already in flight, and
        resetting there would let the same scan be nudged again and again.
        """
        scan_gate["calls"] = 0
        scan_gate["nudged"] = False
        send_text_stimulus(live_request_queue, text)

    # Force the first turn, and pin what it says.
    #
    # This used to send "Neural handshake" and rely on the agent instruction's
    # closing line to produce the greeting. That is a hint, not a contract: the
    # opening line varied between runs, and the UI overlay tells the user to
    # wait for a specific phrase before starting. Asking for the exact words
    # here makes the first thing a demo audience hears the same every time.
    logger.info("Sending opening stimulus to model...")
    if language_code == "en-US":
        opening = 'Initialize now. Say exactly "Scanner Online." and nothing else.'
    else:
        # The model translates its own lines. Shipping translations would mean
        # shipping strings nobody here can check, and the point of the exercise
        # is to show the model generating, not to show our phrasebook.
        opening = (
            f"Initialize now. For this entire session speak only {language_name}, "
            f"including every digit confirmation. Say the {language_name} for "
            f'"Scanner Online." and nothing else.'
        )
    logger.info(f"Opening stimulus in {language_name} ({language_code})")
    put_user_turn(opening)

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
                        if DEBUG_FRAME_DIR:
                            _save_debug_frame(payload, frame_count)
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

                    # Round-trip probe. Echoed straight back without touching
                    # the model, so what it measures is the browser-to-backend
                    # network and nothing else -- which is the point: it splits
                    # the client's `detect` figure into transport and thinking.
                    # The client's own timestamp comes back untouched, so no
                    # clock comparison between the two machines is involved.
                    if json_message.get("type") == "ping":
                        await websocket.send_text(
                            json.dumps(
                                {"type": "pong", "sent_at": json_message.get("sent_at")}
                            )
                        )
                        continue

                    # Extract text from JSON and send to LiveRequestQueue
                    if json_message.get("type") == "text":
                        user_text = json_message.get("text", "")
                        logger.info(f"USER TEXT: {user_text}")
                        put_user_turn(user_text)
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
                    put_user_turn("CONTINUE_SURVEILLANCE")
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

                        # Break a repeat run. Counted on every call, including
                        # the ones the 1.5s dedup above swallows -- those are
                        # exactly the invisible ones, and the count of them is
                        # the whole signal. Once per scan, so a model that
                        # ignores the nudge is not nagged into another loop.
                        scan_gate["calls"] += 1
                        if (
                            scan_gate["calls"] >= STORM_NUDGE_AFTER
                            and not scan_gate["nudged"]
                        ):
                            scan_gate["nudged"] = True
                            logger.warning(
                                f"Repeat run ({scan_gate['calls']} calls for one "
                                f"scan); asking the model to speak"
                            )
                            send_text_stimulus(
                                live_request_queue,
                                f"{count} is recorded. Stop calling report_digit "
                                f"and say the confirmation now.",
                            )
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

            # Transcripts. The field is `input_transcription`, not
            # `input_audio_transcription` -- that is the RunConfig knob that
            # turns transcription on, not the event it produces. This read the
            # config name off the event (and `.final_transcript`, which is not a
            # field of types.Transcription either), so behind getattr's default
            # it silently logged nothing at all for the life of the project.
            # ADK emits partials with finished=False and one accumulated event
            # with finished=True, so gating on `finished` gives one line each.
            input_transcription = getattr(event, "input_transcription", None)
            if input_transcription and input_transcription.finished:
                logger.info(f"USER TRANSCRIPT: {input_transcription.text}")

            output_transcription = getattr(event, "output_transcription", None)
            if output_transcription and output_transcription.finished:
                logger.info(f"GEMINI TRANSCRIPT: {output_transcription.text}")

            # Model turn content (text or audio).
            #
            # This used to probe `event.server_content.model_turn`, which never
            # matched: `serverContent.modelTurn` is the raw Live API wire shape,
            # not ADK's Event shape. Event has no such field and is
            # `extra="ignore"`, so the attribute was silently absent rather than
            # an error -- which is why no log has ever carried a GEMINI TEXT line
            # or an audio-chunk count. The model turn is in `event.content`; the
            # client's own fallback path (`msg.content?.parts` in
            # useGeminiSocket.js) is what has been carrying the audio all along.
            content = getattr(event, "content", None)
            if content and content.parts and content.role != "user":
                for part in content.parts:
                    if part.text:
                        logger.info(f"GEMINI TEXT: {part.text}")
                    if part.inline_data:
                        model_audio_count += 1
                        if model_audio_count % 50 == 0:
                            logger.info(
                                f"Sent model audio chunk #{model_audio_count} to client"
                            )

            # Model audio on its own binary frame, unless explicitly asked for
            # the legacy base64-in-JSON path. The bytes are stripped from the
            # event so nothing is ever sent twice; only events carrying audio
            # pay the rebuild cost.
            audio_parts = (
                [
                    part.inline_data.data
                    for part in (content.parts if content and content.parts else [])
                    if part.inline_data and part.inline_data.data
                ]
                if MODEL_AUDIO_ENCODING != "base64"
                else []
            )
            if audio_parts:
                for chunk in audio_parts:
                    payload_bytes = (
                        pcm16_to_ulaw(chunk)
                        if MODEL_AUDIO_ENCODING == "mulaw"
                        else chunk
                    )
                    await websocket.send_bytes(
                        bytes([MODEL_AUDIO_PREFIX]) + payload_bytes
                    )
                payload = json.loads(
                    event.model_dump_json(exclude_none=True, by_alias=True)
                )
                parts = (payload.get("content") or {}).get("parts")
                if parts:
                    payload["content"]["parts"] = [
                        p for p in parts if not p.get("inlineData")
                    ]
                await websocket.send_text(json.dumps(payload))
            else:
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
        # Full detail rather than str(e): a 1007 close reports "Request
        # contains an invalid argument" without naming the argument, and the
        # code/status/message live on the exception object.
        logger.error(
            f"Error: {type(e).__name__} {e} | "
            f"code={getattr(e, 'code', None)} status={getattr(e, 'status', None)} "
            f"details={getattr(e, 'details', None)} response={getattr(e, 'response', None)}",
            exc_info=True,
        )
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


@app.middleware("http")
async def cache_policy(request, call_next):
    """Never let the browser cache the entry point.

    Vite content-hashes everything under /assets, so those are safe to cache
    forever -- the filename changes when the content does. `index.html` is the
    opposite: same URL, new contents on every build, and it was served with only
    an ETag and no Cache-Control. Browsers apply heuristic caching to that, so a
    normal reload can hand back a stale index.html pointing at the *previous*
    bundle.

    That is not theoretical. It is the likely cause of the silent-audio
    regression on 2026-08-13: the backend had moved model audio to binary
    frames, a cached page was still running the build that only understood
    base64 in the JSON, and the result was no audio and nothing in any log.
    /audio-processor.js has the same exposure -- it is served from public/ and
    is not content-hashed, and its contents changed today.

    It is also why every deploy of this app has come with "hard-refresh first".
    """
    response = await call_next(request)
    if not request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    else:
        # Hashed filename: safe to keep, and worth keeping.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.get("/api/config")
async def get_config() -> dict:
    """Non-secret runtime config, readable without opening a session.

    The same values ride the WebSocket `config` frame, but that only arrives
    once a round starts -- so the UI, which titles itself with the model name,
    showed a placeholder on the idle screen where a viewer first looks at it.
    This is a plain GET so the page can be honest before anything is billed.

    Deliberately not the API key, the project, or anything else from the
    environment: this endpoint is reachable by anyone who can reach the app.
    """
    return {
        "model": MODEL_ID,
        "video_fps": VIDEO_FPS,
        "video_width": VIDEO_WIDTH,
        "video_height": VIDEO_HEIGHT,
        "jpeg_quality": JPEG_QUALITY,
        "response_modality": RESPONSE_MODALITY,
        # The UI builds its language selector from this, so the list cannot
        # drift from what the backend will actually accept.
        "languages": LANGUAGES,
        "default_language": DEFAULT_LANGUAGE,
    }


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
