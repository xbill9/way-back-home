"""
Location Confirmation Tool

This module provides the tool for confirming the explorer's location
and activating the rescue beacon via the backend API.

This is the PLACEHOLDER VERSION. Follow the codelab instructions
to fill in the #REPLACE sections.
"""

#REPLACE-CONFIRM-TOOL
import os
import logging
import requests

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

BIOME_TO_QUADRANT = {
    "CRYO": "NW",
    "VOLCANIC": "NE",
    "BIOLUMINESCENT": "SW",
    "FOSSILIZED": "SE"
}


def _get_actual_biome(x: int, y: int) -> tuple[str, str]:
    """Determine actual biome and quadrant from coordinates."""
    if x < 50 and y >= 50:
        return "NW", "CRYO"
    elif x >= 50 and y >= 50:
        return "NE", "VOLCANIC"
    elif x < 50 and y < 50:
        return "SW", "BIOLUMINESCENT"
    else:
        return "SE", "FOSSILIZED"


def confirm_location(biome: str, tool_context: ToolContext) -> dict:
    """
    Confirm the explorer's location and activate the rescue beacon.
    
    Uses ToolContext to read state values set by before_agent_callback.
    """
    # Read from state (set by before_agent_callback)
    participant_id = tool_context.state.get("participant_id", "")
    x = tool_context.state.get("x", 0)
    y = tool_context.state.get("y", 0)
    backend_url = tool_context.state.get("backend_url", "https://api.waybackhome.dev")
    
    # Fallback to environment variables
    if not participant_id:
        participant_id = os.environ.get("PARTICIPANT_ID", "")
    if not backend_url:
        backend_url = os.environ.get("BACKEND_URL", "https://api.waybackhome.dev")

    if not participant_id:
        return {"success": False, "message": "❌ No participant ID available."}

    biome_upper = biome.upper().strip()

    if biome_upper not in BIOME_TO_QUADRANT:
        return {"success": False, "message": f"❌ Unknown biome: {biome}"}

    # Get actual biome from coordinates
    actual_quadrant, actual_biome = _get_actual_biome(x, y)

    if biome_upper != actual_biome:
        return {
            "success": False,
            "message": f"❌ Mismatch! Analysis: {biome_upper}, Actual: {actual_biome}"
        }

    quadrant = BIOME_TO_QUADRANT[biome_upper]

    try:
        response = requests.patch(
            f"{backend_url}/participants/{participant_id}/location",
            params={"x": x, "y": y},
            timeout=10
        )
        response.raise_for_status()

        return {
            "success": True,
            "message": f"🔦 BEACON ACTIVATED!\n\nLocation: {biome_upper} in {quadrant}\nCoordinates: ({x}, {y})"
        }

    except requests.exceptions.ConnectionError:
        return {
            "success": True,
            "message": f"🔦 BEACON ACTIVATED! (Local)\n\nLocation: {biome_upper} in {quadrant}",
            "simulated": True
        }

    except Exception as e:
        return {"success": False, "message": f"❌ Failed: {str(e)}"}


confirm_location_tool = FunctionTool(confirm_location)
