# agent/tools/hybrid_search_tools.py
"""
Hybrid Search Tools for the Survivor Network Agent.

Combines AI-interpreted keyword search with RAG semantic search
for optimal results based on query type.
"""

from services.hybrid_search_service import (
    HybridSearchService, 
    SearchMethod,
    SearchResult
)
from typing import Optional, List
import os

# Singleton service
_service: Optional[HybridSearchService] = None


def _get_service() -> HybridSearchService:
    """Get or create hybrid search service."""
    global _service
    if _service is None:
        _service = HybridSearchService(
            project_id=os.getenv('PROJECT_ID'),
            instance_id=os.getenv('INSTANCE_ID'),
            database_id=os.getenv('DATABASE_ID')
        )
    return _service


def _format_results(
    results: List[SearchResult],
    analysis: dict,
    show_analysis: bool = True
) -> str:
    """Format search results for display."""
    
    lines = []
    
    if show_analysis:
        lines.extend([
            "## 🧠 Search Strategy",
            f"**Query Analysis:**",
            f"- Recommended Method: `{analysis['recommended_method']}`",
            f"- Actually Used: `{analysis['actual_method']}`",
            f"- Confidence: {analysis['confidence']:.0%}",
        ])
        
        if analysis['keywords_extracted']:
            lines.append(f"- Keywords: {', '.join(analysis['keywords_extracted'][:5])}")
        
        if analysis['categories']:
            lines.append(f"- Categories: {', '.join(analysis['categories'])}")
            
        if analysis['biome_filter']:
            lines.append(f"- Location Filter: {analysis['biome_filter']}")
        
        if analysis['reasoning']:
            lines.append(f"- Reasoning: _{analysis['reasoning']}_")
        
        lines.extend(["", "---", ""])
    
    lines.append("## 📋 Results")
    lines.append("")
    
    if not results:
        lines.append("No results found. Try rephrasing your query.")
        return "\n".join(lines)
    
    for i, r in enumerate(results, 1):
        # Score as percentage
        score_pct = r.score * 100
        
        # Method indicator
        method_emoji = {
            SearchMethod.KEYWORD: "🔤",
            SearchMethod.RAG: "🧬",
            SearchMethod.HYBRID: "🔀"
        }.get(r.method, "•")
        
        lines.append(f"{i}. **{r.name}** ({score_pct:.0f}% match) {method_emoji}")
        
        if r.details.get("biome"):
            lines.append(f"   📍 Location: {r.details['biome']}")
        
        # Show matching skills
        skills = r.details.get("matching_skills", [])
        if skills:
            skill_names = [s["name"] if isinstance(s, dict) else s for s in skills[:5]]
            lines.append(f"   🛠️ Skills: {', '.join(skill_names)}")
        
        # Show which method found this
        if r.details.get("found_by"):
            lines.append(f"   _Found by: {r.details['found_by']}_")
        
        lines.append("")
    
    # Legend
    lines.extend([
        "---",
        "_Legend: 🔤 Keyword | 🧬 Semantic (RAG) | 🔀 Hybrid (Both)_"
    ])
    
    return "\n".join(lines)


async def hybrid_search(query: str, limit: int = 10) -> str:
    """
    Smart search that automatically chooses the best method.
    
    This tool analyzes your query and decides whether to use:
    - **Keyword Search**: When you have specific terms, filters, or categories
    - **RAG Search**: When you need semantic understanding or similarity
    - **Hybrid**: When both approaches would help
    
    The AI is transparent about which method it chose and why!
    
    Examples:
    - "Find medical skills in forest" → Keyword (has filter)
    - "Find skills similar to first aid" → RAG (needs similarity)
    - "Who can help with healing in mountains" → Hybrid (both)
    
    Args:
        query: Your search query in natural language
        limit: Maximum number of results (default: 10)
        
    Returns:
        Formatted results with search strategy explanation
    """
    try:
        service = _get_service()
        result = service.smart_search(query, limit=limit)
        
        return _format_results(
            result["results"],
            result["analysis"],
            show_analysis=True
        )
        
    except Exception as e:
        return f"Error in hybrid search: {str(e)}"


# TODO: REPLACE_SEMANTIC_SEARCH_TOOL
async def semantic_search(query: str, limit: int = 10) -> str:
    """
    Force semantic (RAG) search using embeddings.
    
    Use this when you specifically want to find things by MEANING,
    not just matching keywords. Great for:
    - Finding conceptually similar items
    - Handling vague or abstract queries
    - When exact terms are unknown
    
    Example: "healing abilities" will find "first aid", "surgery", 
    "herbalism" even though no keywords match exactly.
    
    Args:
        query: What you're looking for (describe the concept)
        limit: Maximum results
        
    Returns:
        Semantically similar results ranked by relevance
    """
    try:
        service = _get_service()
        result = service.smart_search(
            query, 
            force_method=SearchMethod.RAG,
            limit=limit
        )
        
        return _format_results(
            result["results"],
            result["analysis"],
            show_analysis=True
        )
        
    except Exception as e:
        return f"Error in semantic search: {str(e)}"


async def keyword_search(query: str, limit: int = 10) -> str:
    """
    Force keyword-based search using AI interpretation.
    
    Use this when you want fast, exact matching based on
    specific terms. The AI will extract keywords from your
    query and search for exact matches.
    
    Best for:
    - Specific category searches ("medical skills")
    - Location-filtered searches ("in the forest")  
    - When you know the exact terms
    
    Args:
        query: Your search query
        limit: Maximum results
        
    Returns:
        Results matching extracted keywords
    """
    try:
        service = _get_service()
        result = service.smart_search(
            query,
            force_method=SearchMethod.KEYWORD,
            limit=limit
        )
        
        return _format_results(
            result["results"],
            result["analysis"],
            show_analysis=True
        )
        
    except Exception as e:
        return f"Error in keyword search: {str(e)}"


async def find_similar_skills(skill_name: str, limit: int = 5) -> str:
    """
    Find skills semantically similar to a given skill.
    
    This is a pure RAG use case - it uses embeddings to find
    skills with similar meanings, even if the names are different.
    
    Example:
    - "first aid" → finds "emergency medicine", "triage", "CPR"
    - "hunting" → finds "tracking", "trapping", "archery"
    
    Args:
        skill_name: The skill to find similar ones to
        limit: How many similar skills to return
        
    Returns:
        List of similar skills with similarity scores
    """
    try:
        service = _get_service()
        results = service.find_similar_skills(skill_name, limit)
        
        if not results:
            return f"No skills found similar to '{skill_name}'"
        
        lines = [
            f"## 🧬 Skills Similar to '{skill_name}'",
            "",
        ]
        
        for i, r in enumerate(results, 1):
            similarity_pct = r["similarity"] * 100
            lines.append(
                f"{i}. **{r['name']}** ({similarity_pct:.1f}% similar)"
            )
            lines.append(f"   Category: {r['category'] or 'Unknown'}")
            lines.append("")
        
        return "\n".join(lines)
        
    except Exception as e:
        return f"Error finding similar skills: {str(e)}"


async def analyze_query(query: str) -> str:
    """
    Show how the AI would analyze a query WITHOUT searching.
    
    Useful for understanding how the hybrid system works and
    debugging unexpected search behavior.
    
    Args:
        query: The query to analyze
        
    Returns:
        Detailed breakdown of the AI's analysis
    """
    try:
        service = _get_service()
        analysis = service.analyze_query(query)
        
        lines = [
            "## 🔍 Query Analysis",
            "",
            f"**Original Query:** {analysis.original_query}",
            "",
            "### Recommended Strategy",
            f"- **Method:** `{analysis.recommended_method.value}`",
            f"- **Confidence:** {analysis.confidence:.0%}",
            f"- **Reasoning:** {analysis.reasoning}",
            "",
            "### Extracted Information",
            f"- **Keywords:** {analysis.keywords or 'None extracted'}",
            f"- **Categories:** {analysis.categories or 'None detected'}",
            f"- **Biome Filter:** {analysis.biome_filter or 'None'}",
            f"- **Needs Similarity Ranking:** {'Yes' if analysis.needs_similarity_ranking else 'No'}",
            f"- **Has Specific Filters:** {'Yes' if analysis.has_specific_filters else 'No'}",
            "",
            "### What This Means",
        ]
        
        if analysis.recommended_method == SearchMethod.KEYWORD:
            lines.append(
                "→ Query has specific filters/terms, keyword search is best"
            )
        elif analysis.recommended_method == SearchMethod.RAG:
            lines.append(
                "→ Query needs semantic understanding, RAG search is best"
            )
        else:
            lines.append(
                "→ Query benefits from both approaches, hybrid search is best"
            )
        
        return "\n".join(lines)
        
    except Exception as e:
        return f"Error analyzing query: {str(e)}"
