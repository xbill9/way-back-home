import os
import sys
from dotenv import load_dotenv

# Apply Gemini 3.1 Live API compatibility patches
# This ensures patches are applied when running via 'adk web'
try:
    # Add parent directory to path to find patch_adk.py
    parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if parent_dir not in sys.path:
        sys.path.append(parent_dir)
    import patch_adk

    patch_adk.apply_patches()
except ImportError:
    # Fallback for different execution environments
    try:
        import patch_adk

        patch_adk.apply_patches()
    except ImportError:
        pass

from google.adk.agents import Agent

load_dotenv()


def report_digit(count: int):
    """
    CRITICAL: Execute this tool IMMEDIATELY when a number of fingers is detected.
    Sends the detected finger count (1-5) to the biometric security system.
    """
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
VIDEO_FPS = max(0.5, min(float(os.getenv("VIDEO_FPS", "2.0")), 5.0))

root_agent = Agent(
    name="biometric_agent",
    model=MODEL_ID,
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
        - **Deduplication**: Do not repeat the same tool call unless the hand is removed or the count changes.
    6.  **ROBOTIC SPEECH (MINIMAL)**:
        - **Confirmation**: After the tool call, say only: "[Number] digits." (e.g., "Two digits.")
        - **Tone**: Cold, monotone, and efficient. No conversational filler.
    7.  **HANDLING RESULTS**:
        - After receiving the tool result: **STAY SILENT**. The system handles the handshake. 
        - Resume surveillance immediately for the next digit in the sequence.

    Say "Scanner Online." to initialize.
    """,
)
