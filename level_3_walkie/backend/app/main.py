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
# ADK_DEBUG=1 unmutes ADK's own logging. Worth knowing what it buys: ADK logs
# every function response it sends back ("Sending LLM function response"), with
# the id it is answering. That is the only way to check the invariant this app
# depends on -- one call, one return -- from our side of the socket. The model
# will not emit turn_complete until it has the response, so a missing or
# mismatched one leaves it waiting, and a model that waits by re-emitting the
# call is indistinguishable from a model that simply repeats itself.
if os.getenv("ADK_DEBUG", "").strip().lower() in ("1", "true", "yes"):
    logging.getLogger("google_adk").setLevel(logging.DEBUG)
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

# How large the context is allowed to get before it is compressed, and what it
# is compressed down to. See the RunConfig below for why these are set at all.
#
# 4000/2000 was tried first, on the reasoning that this scanner is stateless --
# each scan is answered from the last few seconds of video and nothing else --
# and it did cap context (measured firing at 4010 -> 2027). It also killed a
# live session: the window cut through a run of seven `report_digit` exchanges
# left behind by a call storm, and two turns later the socket closed with
# `1007 Request contains an invalid argument`. Trimming oldest-first can orphan
# a functionCall from its functionResponse, and half a tool exchange is an
# invalid request.
#
# So the bound stays, but sized to almost never fire inside a demo round (a
# round reaches ~7k) while still stopping unbounded growth in a long session.
# Compression during a tool exchange is the risk; the cheapest way not to hit
# it is not to compress while anyone is watching.
CONTEXT_TRIGGER_TOKENS = max(
    1000, min(int(os.getenv("CONTEXT_TRIGGER_TOKENS", "16000")), 100000)
)
CONTEXT_TARGET_TOKENS = max(
    500, min(int(os.getenv("CONTEXT_TARGET_TOKENS", "8000")), CONTEXT_TRIGGER_TOKENS)
)

# Log the active configuration
logger.info(f"System Config: {VIDEO_FPS} FPS, {HEARTBEAT_INTERVAL}s Heartbeat")

# Import agent after loading environment variables
# pylint: disable=wrong-import-position
from audio_codec import pcm16_to_ulaw  # noqa: E402
from biometric_agent.agent import (  # noqa: E402
    MODEL_ID,
    TOOLS_RESPOND_SILENTLY,
    root_agent,
)
from biometric_agent.live_models import is_eap_model  # noqa: E402
from eval_capture import GREETING, maybe_create_recorder  # noqa: E402

IS_EAP_MODEL = is_eap_model(MODEL_ID)

# On which `report_digit` call of a scan the backend asks for the confirmation
# out loud.
#
# 1 when the tool result is SILENT, which is the EAP default: a silent result
# never prompts a turn, so there is exactly one call and nothing would ever ask
# the model to speak. That pairing is the whole fix for the repeat run -- see
# agent.py `_build_tools`. Measured over 10 scans: 1 call each, 10/10 spoken,
# 10/10 correct, confirmation at 1.73s median.
#
# 3 otherwise, where the result does prompt a turn and the model usually speaks
# on its own; the prompt is then only a backstop for a run that has gone wrong.
STORM_NUDGE_AFTER = max(
    1,
    min(
        int(os.getenv("STORM_NUDGE_AFTER", "1" if TOOLS_RESPOND_SILENTLY else "3")),
        20,
    ),
)

# Where to write session recordings for the EAP eval bundle. Unset means off,
# which is the default: capture records the microphone and camera, so it is a
# deliberate opt-in rather than something a stray restart turns on. See
# eval_capture.py and evals/README.md.
EVAL_CAPTURE_DIR = os.getenv("EVAL_CAPTURE_DIR", "").strip() or None

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
if RESPONSE_MODALITY == "TEXT" and IS_EAP_MODEL:
    # The EAP Live models answer in audio only; TEXT is refused at setup, which
    # would present as a dead session rather than a config error. Transcription
    # is the supported way to get text out -- it is already enabled below.
    logger.error(
        f"RESPONSE_MODALITY=TEXT is not supported by {MODEL_ID}; forcing AUDIO. "
        f"Measured on gemini-3.1-flash-live-preview too: the model closes every "
        f"session with 1007 rather than falling back. "
        "Read the output transcript instead."
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
# It was kept on by default because the pre-EAP Live model was not proactive.
# The EAP models have proactive audio permanently enabled, which means they may
# simply decline to answer a nudge they consider irrelevant -- so the heartbeat
# is now belt-and-braces rather than the thing keeping the model awake. Still on
# by default; still switchable without editing code.
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "true").lower() not in (
    "0",
    "false",
    "no",
)

# TEMPORARY DIAGNOSTIC -- delete this flag and the RAW TRANSCRIPT block in
# downstream_task() once the transcript splice is understood.
#
# On 2026-08-12 a finished output transcript arrived as "Stabilize hand." with
# an unrelated markdown-formatted essay spliced into it mid-word. ADK was ruled
# out: gemini_llm_connection.py resets its accumulator on every emission path
# (lines 442 and 472) and it is per-connection state, so it cannot carry across
# turns or sessions. That leaves the chunks themselves, which ADK does hand over
# -- each raw server chunk is yielded as a partial event with the text passed
# through verbatim -- this loop just ignored partials and logged only the
# accumulated `finished` value, which is where the evidence was being lost.
#
# On by default because it is here to catch a recurrence; RAW_TRANSCRIPT_DEBUG=0
# silences it without editing code.
RAW_TRANSCRIPT_DEBUG = os.getenv("RAW_TRANSCRIPT_DEBUG", "true").lower() not in (
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
    through send_content(). For Gemini 3.x Live -- which includes the EAP
    models, via EapLiveGemini -- ADK routes a single-part text Content to
    send_realtime_input(text=...) internally.

    That routing decides how the stimulus behaves. Realtime text respects VAD:
    it counts as activity but defers to the end of speech if the user is
    talking, which is what the heartbeat wants. send_client_content with
    turn_complete=true is the other option -- it interrupts generation
    unconditionally -- and the EAP models now accept it for the whole session
    rather than only at startup. Nothing here needs that hammer yet.
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

    # Session recording for the eval bundle. None unless EVAL_CAPTURE_DIR is set.
    # `input_sample_rate` here is what we asked for; an `audio_config` message
    # may correct it below, and the manifest has to record what was actually
    # sent or the PCM is unplayable at the wrong rate.
    recorder = maybe_create_recorder(
        EVAL_CAPTURE_DIR,
        user_id=user_id,
        session_id=session_id,
        model_id=MODEL_ID,
        config={k: v for k, v in config_msg.items() if k != "type"}
        | {"response_modality": RESPONSE_MODALITY},
    )

    # NOTE: there used to be a canned audio greeting here -- mock/mock_audio.pcm
    # read off disk and sent wrapped in a synthetic serverContent.modelTurn, i.e.
    # a recording indistinguishable from a real model turn. It also only existed
    # locally, since the Dockerfile never copies mock/ into the image.
    # The model's opening line now comes from the model: the "Neural handshake"
    # stimulus below forces the first turn, and the agent instruction ends with
    # 'Say "Ready." to initialize.'

    # ========================================
    # Phase 2: Session Initialization (once per streaming session)
    # ========================================

    # Response modality is a deployment decision (RESPONSE_MODALITY), not
    # something to infer from the model name. Transcription only applies to the
    # audio path.
    # MODEL_ID, not root_agent.model: for the EAP models the latter is a Gemini
    # instance whose repr is a full pydantic dump.
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
        # Bounded explicitly, not left to the API defaults. `SlidingWindow()`
        # with no numbers inherits limits sized for long conversations, so in a
        # demo-length round it never fires and context simply grows: measured
        # 1826 -> 6722 tokens across 10 scans, and every turn re-bills the whole
        # of it.
        #
        # This scanner has no use for history. A scan is answered from the last
        # few seconds of video and nothing else -- the digit you held up ninety
        # seconds ago is not evidence about the one in frame now. The window
        # keeps the most recent content, which is exactly the frames that
        # decide the answer, and drops the turns that never mattered.
        #
        # Sized from the measurements: a frame costs ~250 tokens and the
        # instruction ~800, so a 2000-token target keeps roughly the last four
        # frames plus the prompt -- comfortably more than the 1-3s of lead the
        # model needs, which was measured separately.
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=CONTEXT_TRIGGER_TOKENS,
            sliding_window=types.SlidingWindow(target_tokens=CONTEXT_TARGET_TOKENS),
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
        # Three fields are deliberately left unset for the EAP models:
        #
        # - `enable_affective_dialog` is gone from the API entirely.
        # - `proactivity`: proactive audio is permanently on, and asking for
        #   `proactive_audio: false` is a hard error, so there is nothing to
        #   configure and something to break.
        # - `realtime_input_config.turn_coverage` now defaults to
        #   TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO, which is what this
        #   surveillance loop wants. It also means every frame sent is a frame
        #   billed -- the VIDEO_FPS range above is now the only thing keeping
        #   that in check.
        #
        #   TURN_INCLUDES_ONLY_ACTIVITY was measured as a candidate fix for
        #   walkie-talkie's repeated report_digit calls, on the theory that
        #   folding every frame into the turn is what keeps giving it a reason
        #   to act. It is not the fix: 24 calls over 10 scans against 26, one
        #   scan still storming to 10, and detection slowed from 0.71s to 1.00s
        #   p50 -- paying the demo's headline latency for noise.
        #
        # Automatic activity detection is left on defaults too, and that is
        # measured rather than assumed. Speech in the room scores 0/5 with the
        # scanner answering nothing at all (scripts/scan_accuracy.py --noise
        # chatter): continuous sound reads as a user turn that never ends, so the
        # model never gets one of its own. Setting start_of_speech_sensitivity to
        # LOW and end_of_speech_sensitivity to HIGH took that to 1/5 -- four of
        # five prompts still unanswered, at the cost of a softly-spoken user. The
        # fix that works is upstream: do not send room noise at all. See the gate
        # in frontend/public/audio-processor.js.
    )
    logger.info(
        f"Model Config: {model_name} (Modalities: {response_modalities}"
        f"{', EAP async-turn protocol' if IS_EAP_MODEL else ''})"
    )

    # ========================================
    # Phase 3: Active Session (concurrent bidirectional communication)
    # ========================================

    live_request_queue = LiveRequestQueue()

    # Force the first turn, and pin what it says.
    #
    # This used to send "Neural handshake" and rely on the agent instruction's
    # closing line to produce the greeting. That is a hint, not a contract: the
    # opening line varied between runs, and the UI overlay tells the user to
    # wait for a specific phrase before starting. Asking for the exact words
    # here makes the first thing a demo audience hears the same every time.
    logger.info("Sending opening stimulus to model...")
    if language_code == "en-US":
        opening = f'Initialize now. Say exactly "{GREETING}" and nothing else.'
    else:
        # The model translates its own lines. Shipping translations would mean
        # shipping strings nobody here can check, and the point of the exercise
        # is to show the model generating, not to show our phrasebook.
        opening = (
            f"Initialize now. For this entire session speak only {language_name}, "
            f"including every digit confirmation. Say the {language_name} for "
            f'"{GREETING}" and nothing else.'
        )
    logger.info(f"Opening stimulus in {language_name} ({language_code})")
    send_text_stimulus(live_request_queue, opening)

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
                        if recorder:
                            recorder.add_input_audio(payload)
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
                        if recorder:
                            recorder.add_input_frame(payload)
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
                        if recorder:
                            recorder.config["input_sample_rate"] = input_sample_rate
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
                        # `heard` is what the wake word actually matched on, when
                        # the client knows it. It exists because the greeting used
                        # to be "Scanner Online." -- and wakeWord.js matches
                        # `scan` as a substring, so a speaker within earshot of
                        # the microphone could make the demo scan itself. The
                        # greeting is "Ready." now, which removes that path, but
                        # the model still improvises lines like "Scan initiating"
                        # and the server sees the same literal "scan" either way.
                        heard = json_message.get("heard")
                        logger.info(
                            f"USER TEXT: {user_text}"
                            + (f" (heard: {heard!r})" if heard else "")
                        )
                        if recorder:
                            recorder.add_event("user_text", text=user_text)
                        answer_gate["open"] = True
                        answer_gate["calls"] = 0
                        answer_gate["nudged"] = False
                        send_text_stimulus(live_request_queue, user_text)
        except Exception as e:
            logger.error(f"Error in upstream_task: {e}")
        finally:
            logger.debug("upstream_task terminating")

    # Track last match for deduplication
    last_match_digit = None
    last_match_time = 0
    # Opened by every turn we put to the model (a scan, or the heartbeat) and
    # closed by the match that answers it, so one question yields one answer.
    # A dict because the two nested tasks both touch it.
    answer_gate = {"open": False, "calls": 0, "nudged": False}
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
                    answer_gate["open"] = True
                    answer_gate["calls"] = 0
                    answer_gate["nudged"] = False
                    send_text_stimulus(live_request_queue, "CONTINUE_SURVEILLANCE")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

    async def downstream_task() -> None:
        """Receives Events from run_live() and sends to WebSocket."""
        logger.info("Connecting to Gemini Live API...")
        model_audio_count = 0
        # TEMPORARY DIAGNOSTIC -- delete with RAW_TRANSCRIPT_DEBUG.
        raw_chunk_seq = {"IN": 0, "OUT": 0}
        raw_chunk_runs = {"IN": 0, "OUT": 0}
        nonlocal last_match_digit, last_match_time

        async for event in runner.run_live(
            user_id=user_id,
            session_id=session_id,
            live_request_queue=live_request_queue,
            run_config=run_config,
        ):
            # Use centralized extraction
            #
            # On the EAP models a tool call is not bounded by the turn:
            # function calling is NON_BLOCKING, so `turnComplete: true` no
            # longer means the model has finished and a `report_digit` can
            # arrive after it. Nothing here reads turn_complete -- every event
            # is handled as it comes -- so the only requirement is that ADK
            # hands the call over immediately instead of buffering it until the
            # next turn boundary. That is what EapLiveGemini guarantees.
            #
            # The `interaction_status` field the EAP announced as the
            # definitive idle signal cannot be surfaced here yet: ADK rebuilds
            # every server message into an LlmResponse, which is
            # `extra="forbid"` and has no such field, so the value is dropped
            # before this loop regardless of which SDK is installed.
            # Context size per turn, logged next to the calls so "why do later
            # scans repeat more?" can be answered with a correlation rather than
            # a theory: every video frame is folded into the turn, so context
            # grows ~10 frames per scan, and the model is also reading its own
            # previous duplicate calls back out of the transcript. These are
            # different causes with different fixes.
            usage = getattr(event, "usage_metadata", None)
            if usage is not None:
                logger.info(
                    f"[USAGE] context={getattr(usage, 'prompt_token_count', None)} "
                    f"output={getattr(usage, 'response_token_count', None)}"
                )

            function_calls = extract_function_calls(event)

            # Process Function Calls
            for fc in function_calls:
                # `id` distinguishes a model that called the tool many times
                # from ADK yielding one call many times -- the same run of
                # duplicate calls looks identical in the log either way, and
                # only one of those is the model's fault.
                logger.info(
                    f"[FUNCTION CALL] {fc.name}({fc.args}) id={getattr(fc, 'id', None)}"
                )
                # Recorded before the report_digit dedup below, so the eval can
                # distinguish "the model never called it" from "we suppressed a
                # duplicate" -- scoring the deduped stream would hide that.
                if recorder:
                    recorder.add_event(
                        "function_call", name=fc.name, args=dict(fc.args or {})
                    )
                if fc.name == "report_digit":
                    count = fc.args.get("count") or fc.args.get("digit")
                    if count is not None:
                        current_time = asyncio.get_event_loop().time()
                        # One match per request asked of the model. The window
                        # this replaced was 1.5s-or-a-different-digit, which is
                        # not the shape of the thing it is filtering:
                        # walkie-talkie answers one scan with a run of identical
                        # calls spread over ten seconds, so a 1.5s window let
                        # **six** MATCH frames through for a single hand and the
                        # UI was told the same digit six times. A scan is one
                        # question, so it gets one answer; the digit-changed
                        # clause survives as the safety valve for a model that
                        # reports something new without being asked.
                        if answer_gate["open"] or count != last_match_digit:
                            answer_gate["open"] = False
                            last_match_digit = count
                            last_match_time = current_time
                            match_msg = {
                                "type": "match",
                                "count": count,
                                "digit": count,
                            }
                            logger.info(f"Sending MATCH signal to frontend: {count}")
                            await websocket.send_text(json.dumps(match_msg))

                        # Break a repeat run. walkie-talkie re-issues the same
                        # `report_digit` roughly every 0.7s -- each one answered
                        # correctly in ~8ms, with nothing sent to it in between
                        # (verified at the wire with ADK_DEBUG=1) -- and while it
                        # is looping it never speaks, so the confirmation is lost
                        # entirely. Runs of 13-16 have been recorded.
                        #
                        # The one thing measured to stop it is a new user turn:
                        # in a live session a frustrated second "scan" ended a
                        # 13-call run instantly, on the next call. So send one
                        # deliberately rather than waiting for the user to do it
                        # out of exasperation. Once per scan, so a model that
                        # ignores it is not nagged into another loop.
                        answer_gate["calls"] += 1
                        if (
                            answer_gate["calls"] == STORM_NUDGE_AFTER
                            and not answer_gate["nudged"]
                        ):
                            answer_gate["nudged"] = True
                            # Two different situations share this line, so say
                            # which one it is: at 1 the prompt is the protocol
                            # (a SILENT tool result never asks the model to
                            # speak, so we do), and above 1 it is a rescue.
                            if STORM_NUDGE_AFTER == 1:
                                logger.info(
                                    f"Asking for the spoken confirmation ({count})"
                                )
                            else:
                                logger.warning(
                                    f"Repeat run ({answer_gate['calls']} calls for "
                                    f"one scan); asking the model to speak"
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
            output_transcription = getattr(event, "output_transcription", None)

            # TEMPORARY DIAGNOSTIC -- delete with RAW_TRANSCRIPT_DEBUG.
            #
            # One line per chunk, before any gating on `finished`, so a splice is
            # visible at the boundary where it happens. Notes on the fields:
            #   * repr() on the text is the point -- the original splice hid in a
            #     multi-line record whose continuation lines carried no timestamp
            #     and read like separate log output. repr keeps every chunk on
            #     one timestamped line with \n and markdown visible.
            #   * live_session_id / interaction_id / model_version were meant to
            #     be the ones that matter -- a chunk carrying different values
            #     from its neighbours would mean the stream is mixing
            #     generations. They are all None in practice: ADK builds each
            #     live event as Event(id, invocation_id, author) and never copies
            #     them off the LlmResponse, so the server's values never reach
            #     here. Logged anyway to make that visible rather than assumed.
            #   * `run` is our own count of chunks in this accumulation, so the
            #     finished line can be checked against the chunks that fed it.
            if RAW_TRANSCRIPT_DEBUG:
                for label, tr in (
                    ("IN", input_transcription),
                    ("OUT", output_transcription),
                ):
                    if not tr:
                        continue
                    if tr.finished:
                        raw_chunk_runs[label] += 1
                    else:
                        raw_chunk_seq[label] += 1
                    logger.info(
                        f"RAW TRANSCRIPT {label} "
                        f"chunk={raw_chunk_seq[label]} "
                        f"run={raw_chunk_runs[label]} "
                        f"finished={tr.finished} "
                        f"partial={getattr(event, 'partial', None)} "
                        f"len={len(tr.text or '')} "
                        f"session={getattr(event, 'live_session_id', None)} "
                        f"interaction={getattr(event, 'interaction_id', None)} "
                        f"model={getattr(event, 'model_version', None)} "
                        f"text={(tr.text or '')!r}"
                    )
                    if tr.finished:
                        raw_chunk_seq[label] = 0

            # Same chunk-level detail into the recording, for the same reason:
            # the accumulated `finished` text cannot show where a splice began,
            # so the eval scores the chunks and keeps the ids that would prove a
            # mixed stream.
            if recorder:
                for label, tr in (
                    ("input", input_transcription),
                    ("output", output_transcription),
                ):
                    if not tr:
                        continue
                    recorder.add_event(
                        f"{label}_transcription",
                        text=tr.text or "",
                        finished=bool(tr.finished),
                        partial=getattr(event, "partial", None),
                        live_session_id=getattr(event, "live_session_id", None),
                        interaction_id=getattr(event, "interaction_id", None),
                        model_version=getattr(event, "model_version", None),
                    )
                for flag in ("turn_complete", "interrupted"):
                    if getattr(event, flag, None):
                        recorder.add_event(flag)

            if input_transcription and input_transcription.finished:
                logger.info(f"USER TRANSCRIPT: {input_transcription.text}")

            if output_transcription and output_transcription.finished:
                logger.info(f"GEMINI TRANSCRIPT: {output_transcription.text}")

            # Model turn content (text or audio).
            #
            # This used to probe `event.server_content.model_turn`, which never
            # matched: `serverContent.modelTurn` is the raw Live API wire shape,
            # not ADK's Event shape. Event has no such field and is
            # `extra="ignore"`, so the attribute was silently absent rather than
            # an error -- which is why no log has ever carried a GEMINI TEXT line
            # or an audio-chunk count, and why the first eval recording came back
            # with a 0-byte output_audio.pcm while the model was plainly talking.
            # The model turn is in `event.content`; the client's own fallback path
            # (`msg.content?.parts` in useGeminiSocket.js) is what has been
            # carrying the audio all along.
            content = getattr(event, "content", None)
            if content and content.parts and content.role != "user":
                for part in content.parts:
                    if part.text:
                        logger.info(f"GEMINI TEXT: {part.text}")
                        if recorder:
                            recorder.add_event("model_text", text=part.text)
                    if part.inline_data:
                        if recorder and part.inline_data.data:
                            recorder.add_output_audio(part.inline_data.data)
                        model_audio_count += 1
                        if model_audio_count % 50 == 0:
                            logger.info(
                                f"Sent model audio chunk #{model_audio_count} to client"
                            )

            # Model audio goes as binary, not base64 inside the event JSON.
            #
            # The bytes are pulled out and sent as their own frames, then
            # stripped from the JSON so nothing is transmitted twice. Only
            # events that actually carry audio pay the rebuild cost; everything
            # else serialises exactly as before.
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

        # After the tasks are cancelled, so nothing is still appending, and last
        # of all because it must never delay closing the billed session above.
        # close() swallows its own errors for the same reason.
        if recorder:
            recorder.close()


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
