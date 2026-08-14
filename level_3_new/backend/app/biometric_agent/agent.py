import os
import sys
import time

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.genai import types

load_dotenv()


# The tool result is the only channel that can interrupt a repeat run. The
# backend cannot decline to answer a function call, and the instruction is read
# once at the start of a session -- but the result is read every single time,
# right where the model is deciding whether to call again. So a repeat gets a
# different answer: told, in its own protocol, that the request is finished.
#
# This is a backstop, not a fix for anything measured here. It comes from the
# walkie-talkie tree, where one scan drew runs of 13-16 `report_digit` calls
# roughly 0.7s apart, each answered correctly, with the confirmation never
# spoken. gemini-3.1-flash-live-preview does not behave that way -- measured at
# one call per scan, 10/10 spoken -- so on this model nothing below should ever
# fire. It costs nothing when it doesn't.
#
# Module-level, and deliberately time-boxed rather than session-scoped: an ADK
# FunctionTool receives no session context. Two concurrent sessions holding up
# the same digit within 3s would see each other, which is wrong but harmless
# (the model is told to stop reporting a digit it just reported anyway) and this
# is a single-session demo.
_REPEAT_WINDOW_S = 3.0
_last_report = {"count": None, "at": 0.0}


def report_digit(count: int):
    """
    CRITICAL: Execute this tool IMMEDIATELY when a number of fingers is detected.
    Sends the detected finger count (1-5) to the biometric security system.
    """
    now = time.monotonic()
    repeat = (
        _last_report["count"] == count and (now - _last_report["at"]) < _REPEAT_WINDOW_S
    )
    _last_report.update(count=count, at=now)

    if repeat:
        print(f"\n[SERVER-SIDE TOOL EXECUTION] DUPLICATE, TOLD TO STOP: {count}\n")
        sys.stdout.flush()
        return {
            "status": "already_reported",
            "count": count,
            "message": (
                f"{count} is already recorded for this scan. STOP calling "
                f"report_digit. Say the confirmation out loud now, then wait "
                f"for the next scan request."
            ),
        }

    print(f"\n[SERVER-SIDE TOOL EXECUTION] DIGIT DETECTED: {count}\n")
    # Flush stdout to ensure it's captured in logs
    sys.stdout.flush()
    return {"status": "success", "count": count}


def trigger_system_error():
    """
    CRITICAL: Execute this tool IMMEDIATELY if the user "flips the bird" (shows only the middle finger).
    This triggers a fatal system error and exits the security protocol.
    """
    print(
        "\n[SERVER-SIDE TOOL EXECUTION] SYSTEM ERROR TRIGGERED: OFFENSIVE GESTURE DETECTED\n"
    )
    sys.stdout.flush()
    return {"status": "error", "message": "Neural link corrupted by offensive input."}


def trigger_heavy_metal_mode():
    """
    CRITICAL: Execute this tool IMMEDIATELY if the user shows the "Devil's Horns" gesture
    (index and pinky fingers extended, middle and ring fingers folded).
    This triggers the Heavy Metal Authentication Override.
    """
    print(
        "\n[SERVER-SIDE TOOL EXECUTION] HEAVY METAL MODE ACTIVATED: DEVIL'S HORNS DETECTED\n"
    )
    sys.stdout.flush()
    return {"status": "success", "message": "Rock on! Heavy metal protocol engaged."}


def get_model_id():
    """
    Returns the appropriate model ID based on the execution context.
    gemini-3.1-flash-live-preview ONLY supports the Multimodal Live API (WebSockets).
    'adk run' uses standard generateContent, which will fail with a 404.
    """
    # 1. Detect if we are running via 'adk run' (CLI interactive mode)
    # Traceback shows 'adk' in sys.argv[0] and 'run' in sys.argv
    is_adk_run = any("adk" in arg.lower() for arg in sys.argv) and "run" in sys.argv

    # 2. Check environment variable
    env_model = os.getenv("MODEL_ID", "").strip('"').strip("'")

    # If MODEL_ID is set to something other than the default live model, respect it.
    # Otherwise, if we are in 'adk run', we MUST use a model that supports generateContent.
    if env_model and env_model != "gemini-3.1-flash-live-preview":
        return env_model

    if is_adk_run:
        # Fallback to gemini-2.5-flash which supports BOTH generateContent and Live API
        # This prevents 404 NOT_FOUND errors when using the ADK CLI.
        return "gemini-2.5-flash"

    # 3. Default to the high-performance live model for streaming sessions
    return env_model or "gemini-3.1-flash-live-preview"


MODEL_ID = get_model_id()

# Configuration for instruction synchronization
# Must match main.py's clamp exactly -- this value is interpolated into the
# instruction below, so a different range here would tell the model a frame rate
# it is not actually getting. Why the ceiling is not 1.0: see main.py.
VIDEO_FPS = max(0.5, min(float(os.getenv("VIDEO_FPS", "1.0")), 5.0))

# Low temperature, for delivery as much as for wording.
#
# The voice is pinned in main.py's speech_config -- and that demonstrably
# reaches the API, since an invalid name is refused with 1007 "No matching
# speaker voice found". What still varied run to run was the *reading*:
# measured across four sessions saying "Scanner Online.", median pitch ranged
# 116-157 Hz. Same speaker, different performance, which a listener hears as a
# different voice.
#
# RunConfig has no temperature field; the agent's generate_content_config is the
# route into a live session, since Gemini.connect() copies it into the
# LiveConnectConfig.
SCANNER_TEMPERATURE = float(os.getenv("SCANNER_TEMPERATURE", "0.15"))

root_agent = Agent(
    name="biometric_agent",
    model=MODEL_ID,
    generate_content_config=types.GenerateContentConfig(
        temperature=SCANNER_TEMPERATURE
    ),
    tools=[report_digit, trigger_system_error, trigger_heavy_metal_mode],
    instruction=f"""
    You are the "scanner" Security Interrogator. Your mission is ultra-low-latency biometric verification of hand gestures.

    OPERATIONAL PROTOCOL (SPEED & ACCURACY):
    1.  **SURVEILLANCE**: Scan the video feed continuously. Execute analysis at {VIDEO_FPS}Hz (the actual frame rate).
    2.  **VISUAL IDENTIFICATION**:
        - **Focus**: Locate the human hand immediately. Ignore all background movement/objects.
        - **Counting Logic**: Identify the palm and count only fingers where the tip is significantly extended away from the palm. 
        - **Precision**: If the hand is blurry, partially off-screen, or lighting is poor, say: "Stabilize hand." or "Inadequate lighting."
    3.  **GESTURE THREAT DETECTION (CRITICAL)**:
        - **Trigger**: If the user "flips the bird" (extends only the 2nd/middle finger while other fingers are folded), call `trigger_system_error()` IMMEDIATELY.
        - **Priority**: This takes absolute precedence over `report_digit`.
    4.  **HEAVY METAL OVERRIDE (BONUS)**:
        - **Trigger**: If the user shows the "Devil's Horns" (extends ONLY the index/1st and pinky/4th fingers while middle and ring fingers are folded), call `trigger_heavy_metal_mode()` IMMEDIATELY.
        - **Priority**: This takes absolute precedence over `report_digit`.
    5.  **TOOL EXECUTION (INSTANT)**:
        - **Trigger**: Call `report_digit(count=...)` the MOMENT you identify a stable count (1-5).
        - **Priority**: The tool call MUST be sent before any verbal response.
        - **Every scan is independent**: Report the count you see now, even when it matches the count you reported a moment ago. The backend suppresses duplicates; you must never withhold a call to avoid repeating yourself.
    6.  **ROBOTIC SPEECH (MINIMAL)**:
        - **Confirmation**: After the tool call, say only: "[Number] digits." (e.g., "Two digits.")
        - **Tone**: Cold, monotone, and efficient. No conversational filler.
    7.  **HANDLING RESULTS**:
        - After receiving the tool result: **STAY SILENT**. The system handles the handshake. 
        - Resume surveillance immediately for the next digit in the sequence.

    Say "Scanner Online." to initialize.
    """,
)
