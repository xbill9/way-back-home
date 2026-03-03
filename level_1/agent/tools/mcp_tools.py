"""
MCP Tool Connections

This module provides connections to the custom MCP server (location-analyzer)
deployed on Cloud Run.

Connection Pattern: StreamableHTTP
- Uses StreamableHTTPConnectionParams for HTTP-based MCP communication
- The MCP endpoint is at: {MCP_SERVER_URL}/mcp
- This is the CUSTOM MCP pattern (vs. OneMCP which is managed by Google)

The MCP server provides:
- analyze_geological: Soil sample analysis via Gemini Vision
- analyze_botanical: Flora recording analysis via Gemini multimodal

This is the PLACEHOLDER VERSION. Follow the codelab instructions
to fill in the #REPLACE sections.
"""

#REPLACE-MCP-TOOL-CONNECTION
import os
import logging

from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

logger = logging.getLogger(__name__)

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL")

_mcp_toolset = None

def get_mcp_toolset():
    """Get the MCPToolset connected to the location-analyzer server."""
    global _mcp_toolset
    
    if _mcp_toolset is not None:
        return _mcp_toolset
    
    if not MCP_SERVER_URL:
        raise ValueError(
            "MCP_SERVER_URL not set. Please run:\n"
            "  export MCP_SERVER_URL='https://location-analyzer-xxx.a.run.app'"
        )
    
    # FastMCP exposes MCP protocol at /mcp endpoint
    mcp_endpoint = f"{MCP_SERVER_URL}/mcp"
    logger.info(f"[MCP Tools] Connecting to: {mcp_endpoint}")
    
    _mcp_toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=mcp_endpoint,
            timeout=120,  # 2 minutes for Gemini analysis
        )
    )
    
    return _mcp_toolset

def get_geological_tool():
    """Get the geological analysis tool from the MCP server."""
    return get_mcp_toolset()

def get_botanical_tool():
    """Get the botanical analysis tool from the MCP server."""
    return get_mcp_toolset()
