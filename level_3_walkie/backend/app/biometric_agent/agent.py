import os
import sys
import time

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from google.genai import types

from .live_models import (
    build_generate_content_config,
    build_live_model,
    get_model_id,
    is_eap_model,
)

load_dotenv()


# The tool result is the only channel that can interrupt a call storm. The
# backend cannot decline to answer a function call, and the instruction is read
# once at the start of a session -- but the result is read every single time,
# right where the model is deciding whether to call again. So a repeat gets a
# different answer: told, in its own protocol, that the request is finished.
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


MODEL_ID = get_model_id()


# Tools. Bare callables on every model, which is exactly what level_3_new
# sends: BLOCKING is call -> result -> one spoken confirmation, the flow rule 6
# describes. `response_scheduling` is never set here, and each of the three ways
# of setting it was measured against the real endpoint before that was settled.
#
#   SILENT     the result never triggers generation, and the model emits the
#              call with no speech of its own -- so nothing is left to say the
#              confirmation. 3/3 detected, speech="" on every trial, downlink
#              12.9 kbit/s (the greeting and nothing after it). This shipped,
#              and is why the scanner detected digits in silence.
#   WHEN_IDLE  restores the voice and then loops: the result prompts a turn,
#              the model looks again and calls again. 6.4 calls per trial
#              against 3.7, and "Four digits." spoken four times for one hand.
#              main.py's 1.5s dedup hides the calls from the client; it does
#              not dedup the speech.
#   INTERRUPT  the result interrupts generation so the model speaks sooner --
#              in theory the fix for a confirmation stuck behind a run of
#              calls. Measured worse on every axis: 42 calls over 10 scans
#              against 26, storms back to 11-12, 7/10 spoken, and one digit
#              reported wrong (9/10, the only accuracy miss of the evening).
#   anything   clever-chatter refuses the field itself, closing the socket with
#              `1007 Function response scheduling is not supported for this
#              model.` -- so the EAP gate that existed to keep clever-chatter
#              working was the one thing breaking it.
#
# That last one contradicts what this repo believed (clever-chatter *requires*
# NON_BLOCKING and hard-errors on BLOCKING). Measured 2026-08-13; the EAP moved,
# or the invite was wrong. `supports_blocking_function_calls()` now has no
# callers here and is kept only as the record of that claim.
def _tool_scheduling_mode(model_id: str) -> str:
    mode = os.getenv("SCHEDULING", "").strip().upper()
    if not mode and is_eap_model(model_id):
        mode = "SILENT"
    return mode if mode in ("SILENT", "WHEN_IDLE", "INTERRUPT") else ""


def _build_tools(model_id: str = MODEL_ID) -> list[FunctionTool]:
    tools = [
        FunctionTool(report_digit),
        FunctionTool(trigger_system_error),
        FunctionTool(trigger_heavy_metal_mode),
    ]
    # SILENT on the EAP models, and it is derived from the wire trace rather
    # than guessed. Never on the others: gemini-3.1-flash-live-preview is
    # BLOCKING-only and refuses NON_BLOCKING declarations. walkie-talkie ends
    # its turn 4ms after the tool call without speaking; our function response
    # is then what prompts the next turn (median 628ms later), in which it
    # re-reads the same video and calls again. SILENT means "add the result to
    # context, do not trigger generation" -- no trigger, no loop. It measured
    # mute on its own because nothing then prompts the confirmation either, so
    # it only works paired with main.py asking for the confirmation explicitly
    # on the first call (STORM_NUDGE_AFTER, which defaults to 1 when this is
    # SILENT). The two are one design, not two knobs: SILENT alone is a mute
    # scanner, and the prompt alone is a turn nobody needed.
    #
    # Measured, 10 scans: exactly 1 call per scan, 10/10 spoken, 10/10 correct,
    # confirmation at 1.73s median / 2.21s worst -- against 18 calls and 2.90s
    # worst under BLOCKING, and matching 3.1's one-call-per-scan discipline.
    mode = _tool_scheduling_mode(model_id)
    if mode:
        for tool in tools:
            tool.response_scheduling = getattr(types.FunctionResponseScheduling, mode)
    return tools


# True when the tool result will not prompt a turn, so main.py knows it has to
# ask for the spoken confirmation itself.
TOOLS_RESPOND_SILENTLY = _tool_scheduling_mode(MODEL_ID) == "SILENT"


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


# Two EAP-only edits to the prompt, so it stays byte-identical to level_3_new
# on every other model.
#
# walkie-talkie re-calls `report_digit` for a hand that simply stays in frame,
# and it escalates: over 10 scans of the same fixtures, 1 call on the first
# scan, then 3, 6, 6, 10-12, for 65 calls against 3.1-flash-live's 10. Under
# BLOCKING the model may not speak until every call has round-tripped, so the
# confirmation slides 1.31s -> 2.67s -> 4.47s -> 5.13s and then stops arriving
# (5/10 scans silent, against 10/10 on 3.1).
#
# The cause is this prompt, not the model. Rule 1 as written for the turn-based
# models says to analyse *continuously, at the frame rate*, and rule 5 says to
# call the moment a count is identified and never to withhold a repeat. That is
# an instruction to call about once a second for as long as a hand is visible,
# and walkie-talkie's observed calls land 0.5-0.7s apart -- it is obeying. 3.1
# only ever acts inside a user turn, so the same words cost it nothing.
#
# Rewriting rule 1 for EAP so that the REQUEST starts a scan rather than the
# video was the obvious next move, and it was measured and dropped: 40 calls
# over 10 scans and 7/10 spoken, against 32 and 8/10. So rule 1 keeps
# level_3_new's wording.
#
# This clause replaces the rest of that rule's sentence rather than being added
# after it, which was the first attempt and is why it only half worked. The
# sentence used to end "The backend suppresses duplicates; you must never
# withhold a call to avoid repeating yourself" -- and a separate one-call line
# was appended straight after it, so the prompt said both "never withhold a
# repeat" and "exactly one call per scan" in the same breath. Under a live
# camera the model followed the first: 7 calls for one hand, each ~0.7s apart,
# and the confirmation 5.6s late. Two rules that contradict each other are not
# stricter than one, they are just ambiguous.
#
# The distinction that actually matters is kept: repeating a digit across scans
# is required, repeating it within one scan is not.
_SCAN_INDEPENDENCE = (
    """ Reporting the same digit again on a LATER scan request is correct and expected. Calling twice for the SAME scan request is not: once you have reported a count, that request is finished -- say the confirmation and wait to be asked again."""
    if is_eap_model(MODEL_ID)
    else """ The backend suppresses duplicates; you must never withhold a call to avoid repeating yourself."""
)

root_agent = Agent(
    name="biometric_agent",
    # An id, or a model instance when the id is one ADK's registry cannot
    # resolve. See live_models.build_live_model().
    model=build_live_model(MODEL_ID),
    generate_content_config=build_generate_content_config(MODEL_ID),
    tools=_build_tools(),
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
        - **Every scan is independent**: Report the count you see now, even when it matches the count you reported a moment ago.{_SCAN_INDEPENDENCE}
    6.  **ROBOTIC SPEECH (MINIMAL)**:
        - **Confirmation**: After the tool call, say only: "[Number] digits." (e.g., "Two digits.")
        - **Tone**: Cold, monotone, and efficient. No conversational filler.
    7.  **HANDLING RESULTS**:
        - After receiving the tool result: **STAY SILENT**. The system handles the handshake.
        - Resume surveillance immediately for the next digit in the sequence.

    Say "Ready." to initialize.
    """,
)
