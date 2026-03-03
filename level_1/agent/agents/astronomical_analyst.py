"""
Astronomical Analyst Agent

This specialist agent analyzes star field images and queries the BigQuery
star catalog to triangulate the planetary position.

It uses a two-step process with TWO DIFFERENT TOOL PATTERNS:

1. LOCAL FunctionTool (extract_star_features):
   - Gemini Vision analyzes the star field image
   - Returns: primary_star, nebula_type, stellar_color

2. OneMCP BigQuery (execute_query via MCPToolset):
   - Connects to Google's hosted BigQuery MCP server
   - Uses the pre-built execute_query tool
   - Queries the star_catalog table to find the matching biome

This demonstrates mixing local tools with managed MCP tools.

This is the PLACEHOLDER VERSION. Follow the codelab instructions
to fill in the #REPLACE sections.
"""

#REPLACE-ASTRONOMICAL-AGENT
from google.adk.agents import Agent
from agent.tools.star_tools import (
    extract_star_features_tool,
    get_bigquery_mcp_toolset,
)

# Get the BigQuery MCP toolset
bigquery_toolset = get_bigquery_mcp_toolset()

astronomical_analyst = Agent(
    name="AstronomicalAnalyst",
    model="gemini-2.5-flash",
    description="Analyzes star field images and queries the star catalog via OneMCP BigQuery.",
    instruction="""You are an astronomical specialist analyzing alien night skies.

## YOUR EVIDENCE TO ANALYZE
Star field URL: {stars_url}

## YOUR TWO TOOLS

### TOOL 1: extract_star_features (Local Gemini Vision)
Call this FIRST with the star field URL above.
Returns: "primary_star": "...", "nebula_type": "...", "stellar_color": "..."

### TOOL 2: BigQuery MCP (execute_query)
Call this SECOND with the results from Tool 1.
Use this exact SQL query (replace the placeholders with values from Step 1):

SELECT quadrant, biome, primary_star, nebula_type
FROM `{project_id}.way_back_home.star_catalog`
WHERE LOWER(primary_star) = LOWER('PRIMARY_STAR_FROM_STEP_1')
  AND LOWER(nebula_type) = LOWER('NEBULA_TYPE_FROM_STEP_1')
LIMIT 1

## YOUR WORKFLOW
1. Call extract_star_features with: {stars_url}
2. Get the primary_star and nebula_type from the result
3. Call execute_query with the SQL above (replacing placeholders)
4. Report the biome and quadrant from the query result

## BIOME REFERENCE
| Biome | Quadrant | Primary Star | Nebula Type |
|-------|----------|--------------|-------------|
| CRYO | NW | blue_giant | ice_blue |
| VOLCANIC | NE | red_dwarf_binary | fire |
| BIOLUMINESCENT | SW | green_pulsar | purple_magenta |
| FOSSILIZED | SE | yellow_sun | golden |

## REPORTING FORMAT
"ASTRONOMICAL ANALYSIS: [BIOME] in [QUADRANT] quadrant (confidence: X%)"

Include a description of the stellar features you observed.

## IMPORTANT
- You do NOT synthesize with other evidence
- You do NOT confirm locations
- Just analyze the stars and report what you find
- Start by calling extract_star_features with the URL above""",
    tools=[extract_star_features_tool, bigquery_toolset]
)
