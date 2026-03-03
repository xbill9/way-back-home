"""
Star Analysis Tools

This module provides tools for astronomical analysis using TWO different patterns:

1. LOCAL FUNCTION TOOL (extract_star_features):
   - Uses Gemini Vision directly to analyze star field images
   - Returns structured stellar features (primary_star, nebula_type, etc.)

2. OneMCP BigQuery (query via MCPToolset):
   - Connects to Google's hosted BigQuery MCP server
   - Uses the pre-built execute_query tool to query the star_catalog
   - Demonstrates the MANAGED MCP pattern vs. custom MCP

This is the PLACEHOLDER VERSION. Follow the codelab instructions
to fill in the #REPLACE sections.
"""

#REPLACE-STAR-TOOLS
import os
import json
import logging

from google import genai
from google.genai import types as genai_types
from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
import google.auth
import google.auth.transport.requests

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION - Environment variables only
# =============================================================================

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

if not PROJECT_ID:
    logger.warning("[Star Tools] GOOGLE_CLOUD_PROJECT not set")

# Initialize Gemini client for star feature extraction
genai_client = genai.Client(
    vertexai=True,
    project=PROJECT_ID or "placeholder",
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
)

logger.info(f"[Star Tools] Initialized for project: {PROJECT_ID}")

# =============================================================================
# OneMCP BigQuery Connection
# =============================================================================

BIGQUERY_MCP_URL = "https://bigquery.googleapis.com/mcp"

_bigquery_toolset = None

def get_bigquery_mcp_toolset():
    """
    Get the MCPToolset connected to Google's BigQuery MCP server.
    
    This uses OAuth 2.0 authentication with Application Default Credentials.
    The toolset provides access to BigQuery's pre-built MCP tools like:
    - execute_query: Run SQL queries
    - list_datasets: List available datasets
    - get_table_schema: Get table structure
   """
    global _bigquery_toolset
    
    if _bigquery_toolset is not None:
        return _bigquery_toolset
    
    logger.info("[Star Tools] Connecting to OneMCP BigQuery...")
    
    # Get OAuth credentials
    credentials, project_id = google.auth.default(
        scopes=["https://www.googleapis.com/auth/bigquery"]
    )
    
    # Refresh to get a valid token
    credentials.refresh(google.auth.transport.requests.Request())
    oauth_token = credentials.token
    
    # Configure headers for BigQuery MCP
    headers = {
        "Authorization": f"Bearer {oauth_token}",
        "x-goog-user-project": project_id or PROJECT_ID
    }
    
    # Create MCPToolset with StreamableHTTP connection
    _bigquery_toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=BIGQUERY_MCP_URL,
            headers=headers
        )
    )
    
    logger.info("[Star Tools] Connected to BigQuery MCP")
    return _bigquery_toolset


# =============================================================================
# Local FunctionTool: Star Feature Extraction
# =============================================================================
# This is a LOCAL tool that calls Gemini directly - demonstrating that
# you can mix local FunctionTools with MCP tools in the same agent.

STAR_EXTRACTION_PROMPT = """Analyze this alien night sky image and extract stellar features.

Identify:
1. PRIMARY STAR TYPE: blue_giant, red_dwarf, red_dwarf_binary, green_pulsar, yellow_sun, etc.
2. NEBULA TYPE: ice_blue, fire, purple_magenta, golden, etc.
3. STELLAR COLOR: blue_white, red_orange, green_purple, yellow_gold, etc.

Respond ONLY with valid JSON:
{"primary_star": "...", "nebula_type": "...", "stellar_color": "...", "description": "..."}
"""


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Gemini response, handling markdown formatting."""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        return {"error": f"Failed to parse response: {str(e)}"}


def extract_star_features(image_url: str) -> dict:
    """
    Extract stellar features from a star field image using Gemini Vision.
    
    This is a LOCAL FunctionTool - we call Gemini directly, not through MCP.
    The agent will use this alongside the BigQuery MCP tools.
    """
    logger.info(f"[Stars] Extracting features from: {image_url}")
    
    response = genai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            STAR_EXTRACTION_PROMPT,
            genai_types.Part.from_uri(file_uri=image_url, mime_type="image/png")
        ]
    )
    
    result = _parse_json_response(response.text)
    logger.info(f"[Stars] Extracted: primary_star={result.get('primary_star')}")
    return result


# Create the local FunctionTool
extract_star_features_tool = FunctionTool(extract_star_features)
