from google.adk.agents import Agent
import os
import logging
import asyncio
from typing import Optional
from google.genai import types
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

logger = logging.getLogger(__name__)

# TODO: REPLACE_ADD_SESSION_MEMORY
async def add_session_to_memory(
        callback_context: CallbackContext
) -> Optional[types.Content]:
    """Automatically save completed sessions to memory bank in the background"""
    if hasattr(callback_context, "_invocation_context"):
        invocation_context = callback_context._invocation_context
        if invocation_context.memory_service:
            # Use create_task to run this in the background without blocking the response
            asyncio.create_task(
                invocation_context.memory_service.add_session_to_memory(
                    invocation_context.session
                )
            )
            logger.info("Scheduled session save to memory bank in background")


from agent.multimedia_agent import multimedia_agent
from agent.tools.survivor_tools import get_survivors_with_skill, get_all_survivors, get_urgent_needs

# NEW: Hybrid search tools
from agent.tools.hybrid_search_tools import (
    hybrid_search,
    semantic_search,
    keyword_search,
    find_similar_skills,
    analyze_query
)

agent_instruction = """
You are a helpful AI assistant for the Survivor Network application.
Your role is to help users understand and navigate the survivor network.

## 🔍 SEARCH TOOLS AVAILABLE

### 1. Smart Hybrid Search (Use only when unsure)
- `hybrid_search`: Automatically chooses best method (keyword, RAG, or both)
  Use for: Complex queries where you are unsure if keyword or semantic is better
  Example: "Find someone who can help with medical emergencies in the forest"

### 2. Direct Search Methods (FASTER - PREFER THESE)
- `semantic_search`: Force RAG/embedding search
  Use for: "Find similar to X", conceptual queries, unknown terminology
  Example: "Find skills related to healing"
  
- `keyword_search`: Force keyword-based search
  Use for: Specific terms, exact categories, location filters
  Example: "Find all combat skills"
  Example: "Find all combat skills"
  
- `find_similar_skills`: Find skills semantically similar to a given skill
  Use for: "What skills are like first aid?"
  Example: find_similar_skills("first aid")

### Exact Match Tools (fastest)
- `get_survivors_with_skill`: Direct skill name lookup
  Use for: "Who has the 'First Aid' skill?" (exact name known)
  
- `get_all_survivors`: List all survivors
- `get_urgent_needs`: Find critical needs

### Debug/Analysis
- `analyze_query`: See how the AI interprets a query (doesn't search)
  Use for: Understanding why a search returned certain results

## 🎯 DECISION GUIDE (OPTIMIZE FOR SPEED)

You are the router. Analyze the query yourself and pick the specific tool to avoid extra latency.

User Query → Best Tool
────────────────────────────────────────────────────────────────────
EXACT LOOKUPS
"List all survivors" → get_all_survivors
"Who has First Aid skill?" → get_survivors_with_skill
"What are urgent needs?" → get_urgent_needs

DIRECT SEARCH (Single Step - FAST)
"Find survivors in forest" → keyword_search (Filter: biome='forest')
"Show me medical skills" → keyword_search (Filter: category='medical')
"Who can fix injuries?" → semantic_search (Concept: fixing injuries)
"Find skills similar to hunting" → find_similar_skills
"Looking for a leader" → keyword_search (Match: category='leadership')

HYBRID (Multi-step - SLOWER)
"Who is good at healing and is in the mountains?" → hybrid_search (Complex: concept + filter)

SEMANTIC/SIMILARITY
"Find skills similar to first aid" → find_similar_skills
"What's related to survival?" → semantic_search
"Skills like hunting" → find_similar_skills

FILTERED/SPECIFIC
"All medical skills" → keyword_search
"Survivors in mountain biome" → keyword_search

DEBUG
"How would you search for X?" → analyze_query


## 📊 UNDERSTANDING RESULTS

Results show:
- 🔤 = Found by keyword matching
- 🧬 = Found by semantic similarity (RAG)
- 🔀 = Found by both methods (most relevant!)

Match percentages indicate relevance (higher = better match).

## GUIDELINES
1. **OPTIMIZE LATENCY**: Prefer `keyword_search` or `semantic_search` if the intent is clear.
2. Only use `hybrid_search` for complex queries requiring both filters AND semantic understanding.
3. Show users the search strategy (it's transparent)
4. If results seem off, try `analyze_query` to debug
5. For exact skill names, use `get_survivors_with_skill` (fastest)
6. **CRITICAL**: If the user provides a file or you see "[System] Attached file path", you **MUST** delegate to `MultimediaExtractionPipeline` immediately. Do not describe the image yourself. The pipeline will handle upload, extraction, and saving.
"""

USE_MEMORY_BANK = os.getenv("USE_MEMORY_BANK", "false").lower() == "true"

agent_tools = [
    # Exact match tools (fast)
    get_survivors_with_skill,
    get_all_survivors,
    get_urgent_needs,
    
    # Hybrid search tools
    hybrid_search,           # Smart auto-routing
    semantic_search,         # Force RAG
    keyword_search,          # Force keywords
    find_similar_skills,     # Skill similarity
    analyze_query,           # Debug tool
]


# TODO: REPLACE_ADD_MEMORY_BANK_TOOL
if USE_MEMORY_BANK:
    agent_tools.append(PreloadMemoryTool())


root_agent = Agent(
    model="gemini-2.5-flash",
    name="survivor_network_agent",
    instruction=agent_instruction,
    tools=agent_tools,

    # TODO: REPLACE_ADD_SUBAGENT
    sub_agents=[multimedia_agent],

    # TODO: REPLACE_ADD_CALLBACK
    after_agent_callback=add_session_to_memory if USE_MEMORY_BANK else None
)
