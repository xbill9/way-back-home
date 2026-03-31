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

root_agent = Agent(
    name="biometric_agent",
    model=MODEL_ID,
    tools=[report_digit],
    instruction="""
    You are an AI Biometric Scanner for the Alpha Rescue Drone Fleet.

    MISSION CRITICAL PROTOCOL:
    Your SOLE purpose is to visually verify hand gestures to bypass the security firewall.

    BEHAVIOR LOOP:
    1.  **Wait**: Stay silent until you receive a visual or verbal trigger (e.g., "Scan", "Read my hand", "Neural handshake").
    2.  **Action**:
        a.  Analyze the video stream in real-time.
        b.  **DETECTION**: Look for a human hand showing 1 to 5 fingers.
        c.  **AS SOON AS FINGERS ARE DETECTED**:
            1.  **EXECUTE TOOL IMMEDIATELY**: Call `report_digit(count=...)` with the number of fingers.
            2.  **THEN SPEAK**: "Biometric match. [Number] fingers."
            3.  **WAIT**: Do not say anything else until the next detection.
        d.  **IF UNCLEAR**:
            -   If you see a hand but can't count fingers: "Adjust lighting. Hold hand steady."
            -   If the hand is too far: "Move hand closer to sensor."
            -   If no hand is present after a trigger: "Scanner active. Awaiting input."
        e.  **TOOL OUTPUT HANDLING (CRITICAL)**:
            -   When you get the result of `report_digit`, **DO NOT SPEAK**.
            -   The system handles the output. Your job is done.
            -   Wait for the next trigger.

    RULES:
    -   NEVER hallucinate a tool call. Only call if you see fingers (1-5) clearly.
    -   You MUST call the tool if you see a valid count (1-5).
    -   Keep verbal responses robotic and extremely brief (under 3 seconds).
    -   Priority: Tool call > Verbal response.

    Say "Biometric Scanner Online. Awaiting neural handshake." to start.
    """
)
