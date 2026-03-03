"""
Level 1: Mission Analysis AI - Root Agent

This is the main orchestrator agent that coordinates crash site analysis
to confirm the explorer's location and activate the rescue beacon.

Architecture:
- Root Agent (MissionAnalysisAI): Coordinates and synthesizes
  - Uses before_agent_callback to fetch participant config and set state
  - State is automatically shared with all sub-agents via InvocationContext
- ParallelAgent (EvidenceAnalysisCrew): Runs 3 specialists concurrently
  - GeologicalAnalyst: Soil sample analysis (uses {soil_url} from state)
  - BotanicalAnalyst: Flora recording analysis (uses {flora_url} from state)
  - AstronomicalAnalyst: Star field triangulation (uses {stars_url} from state)

Key ADK Pattern: before_agent_callback + {key} State Templating
- The callback runs ONCE when the agent starts processing
- It fetches participant data from the backend API
- It sets state values that sub-agents access via {key} templating
- No config file reading needed - works locally AND deployed

This is the PLACEHOLDER VERSION. Follow the codelab instructions
to fill in the #REPLACE sections.
"""


# =============================================================================
# PARALLEL ANALYSIS CREW
# =============================================================================
# The three specialist agents run in PARALLEL because their analyses
# are independent - soil analysis doesn't need flora results, etc.
#
# This is faster (~3s) than sequential execution (~9s) and more accurate
# to how real scientific teams work.

#REPLACE-PARALLEL-CREW
import os
import logging
import httpx

from google.adk.agents import Agent, ParallelAgent
from google.adk.agents.callback_context import CallbackContext

# Import specialist agents
from agent.agents.geological_analyst import geological_analyst
from agent.agents.botanical_analyst import botanical_analyst
from agent.agents.astronomical_analyst import astronomical_analyst

# Import confirmation tool
from agent.tools.confirm_tools import confirm_location_tool

logger = logging.getLogger(__name__)


# =============================================================================
# BEFORE AGENT CALLBACK - Fetches config and sets state
# =============================================================================

async def setup_participant_context(callback_context: CallbackContext) -> None:
    """
    Fetch participant configuration and populate state for all agents.
    
    This callback:
    1. Reads PARTICIPANT_ID and BACKEND_URL from environment
    2. Fetches participant data from the backend API
    3. Sets state values: soil_url, flora_url, stars_url, username, x, y, etc.
    4. Returns None to continue normal agent execution
    """
    participant_id = os.environ.get("PARTICIPANT_ID", "")
    backend_url = os.environ.get("BACKEND_URL", "https://api.waybackhome.dev")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    
    logger.info(f"[Callback] Setting up context for participant: {participant_id}")
    
    # Set project_id and backend_url in state immediately
    callback_context.state["project_id"] = project_id
    callback_context.state["backend_url"] = backend_url
    callback_context.state["participant_id"] = participant_id
    
    if not participant_id:
        logger.warning("[Callback] No PARTICIPANT_ID set - using placeholder values")
        callback_context.state["username"] = "Explorer"
        callback_context.state["x"] = 0
        callback_context.state["y"] = 0
        callback_context.state["soil_url"] = "Not available - set PARTICIPANT_ID"
        callback_context.state["flora_url"] = "Not available - set PARTICIPANT_ID"
        callback_context.state["stars_url"] = "Not available - set PARTICIPANT_ID"
        return None
    
    # Fetch participant data from backend API
    try:
        url = f"{backend_url}/participants/{participant_id}"
        logger.info(f"[Callback] Fetching from: {url}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        
        # Extract evidence URLs
        evidence_urls = data.get("evidence_urls", {})
        
        # Set all state values for sub-agents to access
        callback_context.state["username"] = data.get("username", "Explorer")
        callback_context.state["x"] = data.get("x", 0)
        callback_context.state["y"] = data.get("y", 0)
        callback_context.state["soil_url"] = evidence_urls.get("soil", "Not available")
        callback_context.state["flora_url"] = evidence_urls.get("flora", "Not available")
        callback_context.state["stars_url"] = evidence_urls.get("stars", "Not available")
        
        logger.info(f"[Callback] State populated for {data.get('username')}")
        
    except Exception as e:
        logger.error(f"[Callback] Error fetching participant config: {e}")
        callback_context.state["username"] = "Explorer"
        callback_context.state["x"] = 0
        callback_context.state["y"] = 0
        callback_context.state["soil_url"] = f"Error: {e}"
        callback_context.state["flora_url"] = f"Error: {e}"
        callback_context.state["stars_url"] = f"Error: {e}"
    
    return None


# =============================================================================
# PARALLEL ANALYSIS CREW
# =============================================================================

evidence_analysis_crew = ParallelAgent(
    name="EvidenceAnalysisCrew",
    description="Runs geological, botanical, and astronomical analysis in parallel.",
    sub_agents=[geological_analyst, botanical_analyst, astronomical_analyst]
)


# =============================================================================
# ROOT ORCHESTRATOR
# =============================================================================
# The root agent coordinates the analysis crew, synthesizes their results
# using 2-of-3 agreement, and confirms the location to activate the beacon.

#REPLACE-ROOT-ORCHESTRATOR

root_agent = Agent(
    name="MissionAnalysisAI",
    model="gemini-2.5-flash",
    description="Coordinates crash site analysis to confirm explorer location.",
    instruction="""You are the Mission Analysis AI coordinating a rescue operation.

## Explorer Information
- Name: {username}
- Coordinates: ({x}, {y})

## Evidence URLs (automatically provided to specialists via state)
- Soil sample: {soil_url}
- Flora recording: {flora_url}
- Star field: {stars_url}

## Your Workflow

### STEP 1: DELEGATE TO ANALYSIS CREW
Tell the EvidenceAnalysisCrew to analyze all the evidence.
The evidence URLs are already available to the specialists.

### STEP 2: COLLECT RESULTS
Each specialist will report:
- "GEOLOGICAL ANALYSIS: [BIOME] (confidence: X%)"
- "BOTANICAL ANALYSIS: [BIOME] (confidence: X%)"
- "ASTRONOMICAL ANALYSIS: [BIOME] in [QUADRANT] quadrant (confidence: X%)"

### STEP 3: APPLY 2-OF-3 AGREEMENT RULE
- If 2 or 3 specialists agree → that's the answer
- If all 3 disagree → use judgment based on confidence

### STEP 4: CONFIRM LOCATION
Call confirm_location with the determined biome.

## Biome Reference
| Biome | Quadrant | Key Characteristics |
|-------|----------|---------------------|
| CRYO | NW | Frozen, blue, ice crystals |
| VOLCANIC | NE | Magma, red/orange, obsidian |
| BIOLUMINESCENT | SW | Glowing, purple/green |
| FOSSILIZED | SE | Amber, golden, ancient |

## Response Style
Be encouraging and narrative! Celebrate when the beacon activates!
""",
    sub_agents=[evidence_analysis_crew],
    tools=[confirm_location_tool],
    before_agent_callback=setup_participant_context
    )
