# services/hybrid_search_service.py
"""
Hybrid Search Service combining AI-Interpreted Keywords + RAG Semantic Search.

Uses your existing Spanner setup with:
- TextEmbeddings model (text-embedding-004)
- GeminiPro model (gemini-2.5-pro)
- skill_embedding column in Skills table
"""

from google.cloud import spanner
from google.cloud.spanner_v1 import param_types
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json


class SearchMethod(Enum):
    """Which search method to use"""
    KEYWORD = "keyword"      # AI-interpreted keyword search
    RAG = "rag"              # Semantic embedding search
    HYBRID = "hybrid"        # Both methods combined
    EXACT = "exact"          # Direct exact match (fastest)


@dataclass
class SearchResult:
    """Unified search result from any method"""
    id: str
    name: str
    type: str                          # "survivor", "skill", "resource"
    score: float                       # 0-1, higher = more relevant
    method: SearchMethod               # Which method found this
    details: Dict[str, Any] = field(default_factory=dict)
    
    def __hash__(self):
        return hash(self.id)
    
    def __eq__(self, other):
        return self.id == other.id


@dataclass
class QueryAnalysis:
    """AI's analysis of how to handle a query"""
    original_query: str
    recommended_method: SearchMethod
    keywords: List[str]
    categories: List[str]
    biome_filter: Optional[str]
    needs_similarity_ranking: bool
    has_specific_filters: bool
    confidence: float
    reasoning: str


class HybridSearchService:
    """
    Hybrid search combining keyword and RAG approaches.
    
    Flow:
    ┌─────────────────────────────────────────────────────────────────┐
    │                     HybridSearchService                         │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  1. ANALYZE: AI determines best search strategy                 │
    │     ┌───────────────────────────────────────────────────────┐  │
    │     │  Input: "Find similar skills to first aid"            │  │
    │     │  Output: {method: RAG, needs_similarity: true}        │  │
    │     └───────────────────────────────────────────────────────┘  │
    │                              │                                  │
    │                              ▼                                  │
    │  2. ROUTE: Execute appropriate search method(s)                 │
    │     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
    │     │   KEYWORD    │  │     RAG      │  │    HYBRID    │       │
    │     │  SQL LIKE    │  │  Embeddings  │  │    Both      │       │
    │     └──────────────┘  └──────────────┘  └──────────────┘       │
    │                              │                                  │
    │                              ▼                                  │
    │  3. MERGE: Combine and rank results                            │
    │     ┌───────────────────────────────────────────────────────┐  │
    │     │  Deduplicate, normalize scores, rank by relevance     │  │
    │     └───────────────────────────────────────────────────────┘  │
    │                                                                 │
    └─────────────────────────────────────────────────────────────────┘
    """
    
    def __init__(
        self,
        project_id: str = "neon-emitter-458622-e3",
        instance_id: str = "survivor-network",
        database_id: str = "survivor-db"
    ):
        self.project_id = project_id
        self.client = spanner.Client(project=project_id)
        self.instance = self.client.instance(instance_id)
        self.database = self.instance.database(database_id)
        
        # Cache for known values
        self._known_skills: Optional[List[str]] = None
        self._known_categories: Optional[List[str]] = None
        self._known_biomes: Optional[List[str]] = None
    
    # =========================================================================
    # QUERY ANALYSIS - Determine best search method
    # =========================================================================
    
    def analyze_query(self, query: str) -> QueryAnalysis:
        """
        Use AI to analyze query and determine optimal search strategy.
        
        The AI considers:
        - Does query have specific filters? → Keyword is better
        - Does query ask for "similar" things? → RAG is better
        - Is it open-ended semantic? → RAG is better
        - Does it need exact matches? → Keyword is better
        """
        
        # Load known values for context
        self._load_known_values()
        
        prompt = f"""You are a search strategy optimizer. Analyze this query and determine 
the best search approach.

## User Query
"{query}"

## Available Database Values
Skills (sample): {json.dumps(self._known_skills[:30] if self._known_skills else [])}
Categories: {json.dumps(self._known_categories or [])}
Biomes: {json.dumps(self._known_biomes or [])}

## Search Methods Available

1. KEYWORD: AI extracts keywords → SQL LIKE/IN search
   Best for: Specific filters, exact categories, location-based queries
   Example: "Find medical skills in forest" → keywords=["medical"], biome="forest"

2. RAG: Embed query → Vector similarity search  
   Best for: Semantic similarity, "find similar to X", open-ended queries
   Example: "Find skills similar to first aid" → needs embedding comparison

3. HYBRID: Run both methods, merge results
   Best for: Complex queries that benefit from both approaches
   Example: "Find someone with healing abilities in the mountain area"

4. EXACT: Direct match (handled separately, don't recommend this)

## Output Format (JSON only, no markdown):
{{
    "recommended_method": "keyword" | "rag" | "hybrid",
    "keywords": ["term1", "term2"],
    "categories": ["cat1"],
    "biome_filter": "forest" | null,
    "needs_similarity_ranking": true/false,
    "has_specific_filters": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation of why this method"
}}

## Decision Guidelines
- "similar to", "like", "related to" → RAG
- Specific biome/location mentioned → KEYWORD (can filter exactly)
- Category mentioned (medical, combat) → KEYWORD  
- Vague/abstract concepts → RAG
- Multiple criteria → HYBRID
- Simple lookup → KEYWORD

Analyze the query:"""

        result = self._call_gemini(prompt)
        
        try:
            # Parse JSON response
            clean_json = result.strip()
            if clean_json.startswith("```"):
                lines = clean_json.split("\n")
                clean_json = "\n".join(lines[1:-1])
            
            parsed = json.loads(clean_json)
            
            method_map = {
                "keyword": SearchMethod.KEYWORD,
                "rag": SearchMethod.RAG,
                "hybrid": SearchMethod.HYBRID
            }
            
            return QueryAnalysis(
                original_query=query,
                recommended_method=method_map.get(
                    parsed.get("recommended_method", "hybrid"), 
                    SearchMethod.HYBRID
                ),
                keywords=parsed.get("keywords", []),
                categories=parsed.get("categories", []),
                biome_filter=parsed.get("biome_filter"),
                needs_similarity_ranking=parsed.get("needs_similarity_ranking", False),
                has_specific_filters=parsed.get("has_specific_filters", False),
                confidence=parsed.get("confidence", 0.5),
                reasoning=parsed.get("reasoning", "")
            )
            
        except (json.JSONDecodeError, KeyError) as e:
            # Fallback to hybrid
            return QueryAnalysis(
                original_query=query,
                recommended_method=SearchMethod.HYBRID,
                keywords=query.lower().split()[:5],
                categories=[],
                biome_filter=None,
                needs_similarity_ranking=True,
                has_specific_filters=False,
                confidence=0.3,
                reasoning=f"Fallback to hybrid due to parsing error: {e}"
            )
    
    # =========================================================================
    # KEYWORD SEARCH - AI-Interpreted
    # =========================================================================
    
    def keyword_search(
        self,
        analysis: QueryAnalysis,
        limit: int = 10
    ) -> List[SearchResult]:
        """
        Perform keyword-based search using AI-extracted terms.
        
        Uses traditional SQL with LIKE clauses - no embeddings needed.
        """
        
        results = []
        
        # Build SQL dynamically
        conditions = []
        params = {}
        param_types_dict = {}
        
        # Keyword matching
        if analysis.keywords:
            kw_conditions = []
            for i, kw in enumerate(analysis.keywords[:10]):
                param_name = f"kw{i}"
                kw_conditions.append(
                    f"(LOWER(sk.name) LIKE @{param_name} OR "
                    f"LOWER(sk.category) LIKE @{param_name})"
                )
                params[param_name] = f"%{kw.lower()}%"
                param_types_dict[param_name] = param_types.STRING
            
            if kw_conditions:
                conditions.append(f"({' OR '.join(kw_conditions)})")
        
        # Category filter
        if analysis.categories:
            cat_conditions = []
            for i, cat in enumerate(analysis.categories):
                param_name = f"cat{i}"
                cat_conditions.append(f"LOWER(sk.category) = @{param_name}")
                params[param_name] = cat.lower()
                param_types_dict[param_name] = param_types.STRING
            
            if cat_conditions:
                conditions.append(f"({' OR '.join(cat_conditions)})")
        
        # Biome filter
        if analysis.biome_filter:
            conditions.append("LOWER(s.biome) LIKE @biome")
            params["biome"] = f"%{analysis.biome_filter.lower()}%"
            param_types_dict["biome"] = param_types.STRING
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        sql = f"""
            SELECT
                s.survivor_id,
                s.name AS survivor_name,
                s.biome,
                sk.skill_id,
                sk.name AS skill_name,
                sk.category
            FROM Survivors s
            JOIN SurvivorHasSkill shs ON s.survivor_id = shs.survivor_id
            JOIN Skills sk ON shs.skill_id = sk.skill_id
            WHERE {where_clause}
            ORDER BY s.name, sk.name
            LIMIT @limit
        """
        
        params["limit"] = limit
        param_types_dict["limit"] = param_types.INT64
        
        def run_query(transaction):
            rows = transaction.execute_sql(
                sql, params=params, param_types=param_types_dict
            )
            
            # Group by survivor
            survivor_map = {}
            for row in rows:
                surv_id, surv_name, biome, skill_id, skill_name, category = row
                
                if surv_id not in survivor_map:
                    survivor_map[surv_id] = {
                        "name": surv_name,
                        "biome": biome,
                        "skills": []
                    }
                survivor_map[surv_id]["skills"].append({
                    "id": skill_id,
                    "name": skill_name,
                    "category": category
                })
            
            # Convert to results
            for surv_id, data in survivor_map.items():
                # Score based on number of matching skills
                score = min(len(data["skills"]) / 5.0, 1.0)
                
                results.append(SearchResult(
                    id=surv_id,
                    name=data["name"],
                    type="survivor",
                    score=score,
                    method=SearchMethod.KEYWORD,
                    details={
                        "biome": data["biome"],
                        "matching_skills": data["skills"],
                        "match_count": len(data["skills"])
                    }
                ))
        
        self.database.run_in_transaction(run_query)
        
        # Sort by score
        results.sort(key=lambda x: x.score, reverse=True)
        
        return results
    
    # =========================================================================
    # RAG SEARCH - Semantic Embeddings
    # =========================================================================
    
    def rag_search(
        self,
        query: str,
        limit: int = 10
    ) -> List[SearchResult]:
        """
        Perform semantic search using embeddings.
        
        Uses your existing setup:
        - TextEmbeddings model
        - skill_embedding column
        - COSINE_DISTANCE function
        """
        
        results = []
        
       # TODO: REPLACE_SQL
               # This is your working query from the successful run!
        sql = """
            WITH query_embedding AS (
                SELECT embeddings.values AS val
                FROM ML.PREDICT(
                    MODEL TextEmbeddings,
                    (SELECT @query AS content)
                )
            )
            SELECT
                s.survivor_id,
                s.name AS survivor_name,
                s.biome,
                sk.skill_id,
                sk.name AS skill_name,
                sk.category,
                COSINE_DISTANCE(
                    sk.skill_embedding, 
                    (SELECT val FROM query_embedding)
                ) AS distance
            FROM Survivors s
            JOIN SurvivorHasSkill shs ON s.survivor_id = shs.survivor_id
            JOIN Skills sk ON shs.skill_id = sk.skill_id
            WHERE sk.skill_embedding IS NOT NULL
            ORDER BY distance ASC
            LIMIT @limit
        """
        
        def run_query(transaction):
            rows = transaction.execute_sql(
                sql,
                params={"query": query, "limit": limit},
                param_types={
                    "query": param_types.STRING,
                    "limit": param_types.INT64
                }
            )
            
            # Group by survivor, keeping best skill match
            survivor_map = {}
            for row in rows:
                surv_id, surv_name, biome, skill_id, skill_name, category, distance = row
                
                if surv_id not in survivor_map:
                    survivor_map[surv_id] = {
                        "name": surv_name,
                        "biome": biome,
                        "best_distance": float(distance),
                        "skills": []
                    }
                else:
                    # Track best (lowest) distance
                    survivor_map[surv_id]["best_distance"] = min(
                        survivor_map[surv_id]["best_distance"],
                        float(distance)
                    )
                
                survivor_map[surv_id]["skills"].append({
                    "id": skill_id,
                    "name": skill_name,
                    "category": category,
                    "similarity": 1 - float(distance)  # Convert to similarity
                })
            
            # Convert to results
            for surv_id, data in survivor_map.items():
                # Score is 1 - distance (so higher = more similar)
                score = 1 - data["best_distance"]
                
                results.append(SearchResult(
                    id=surv_id,
                    name=data["name"],
                    type="survivor",
                    score=score,
                    method=SearchMethod.RAG,
                    details={
                        "biome": data["biome"],
                        "matching_skills": sorted(
                            data["skills"], 
                            key=lambda x: x["similarity"], 
                            reverse=True
                        ),
                        "best_similarity": score
                    }
                ))
        
        self.database.run_in_transaction(run_query)
        
        # Sort by score (highest first)
        results.sort(key=lambda x: x.score, reverse=True)
        
        return results
    
    # =========================================================================
    # HYBRID SEARCH - Combine Both Methods
    # =========================================================================
    
    def hybrid_search(
        self,
        query: str,
        analysis: QueryAnalysis,
        limit: int = 10
    ) -> List[SearchResult]:
        """
        Run both keyword and RAG search, merge results.
        
        Merging Strategy:
        ┌─────────────────────────────────────────────────────────────┐
        │  1. Run both searches in parallel (conceptually)            │
        │  2. Deduplicate by survivor ID                              │
        │  3. Combine scores: final = 0.4*keyword + 0.6*rag           │
        │     (RAG weighted higher for semantic understanding)        │
        │  4. Merge skill details from both sources                   │
        │  5. Sort by combined score                                  │
        └─────────────────────────────────────────────────────────────┘
        """
        
        # Run both searches
        keyword_results = self.keyword_search(analysis, limit=limit)
        rag_results = self.rag_search(query, limit=limit)
        
        # Create lookup maps
        keyword_map = {r.id: r for r in keyword_results}
        rag_map = {r.id: r for r in rag_results}
        
        # Get all unique IDs
        all_ids = set(keyword_map.keys()) | set(rag_map.keys())
        
        # Create rank maps (ID -> Rank, 1-based)
        keyword_ranks = {r.id: i + 1 for i, r in enumerate(keyword_results)}
        rag_ranks = {r.id: i + 1 for i, r in enumerate(rag_results)}
        
        # Constants
        K = 60
        
        merged_results = []
        
        for surv_id in all_ids:
            kw_result = keyword_map.get(surv_id)
            rag_result = rag_map.get(surv_id)
            
            # Calculate RRF Score
            rank_kw = keyword_ranks.get(surv_id, float('inf'))
            rank_rag = rag_ranks.get(surv_id, float('inf'))
            
            rrf_score = 0.0
            if rank_kw != float('inf'):
                rrf_score += 1.0 / (K + rank_kw)
            if rank_rag != float('inf'):
                rrf_score += 1.0 / (K + rank_rag)
            
            combined_score = rrf_score
            
            # Determine method for display
            if rank_kw != float('inf') and rank_rag != float('inf'):
                method = SearchMethod.HYBRID
            elif rank_rag != float('inf'):
                method = SearchMethod.RAG
            else:
                method = SearchMethod.KEYWORD
            
            # Merge details
            base_result = rag_result or kw_result
            merged_details = dict(base_result.details)
            
            if kw_result and rag_result:
                # Combine skill lists, remove duplicates
                kw_skills = {s["id"]: s for s in kw_result.details.get("matching_skills", [])}
                rag_skills = {s["id"]: s for s in rag_result.details.get("matching_skills", [])}
                
                # Merge, preferring RAG skills (have similarity scores)
                all_skills = {**kw_skills, **rag_skills}
                merged_details["matching_skills"] = list(all_skills.values())
                merged_details["found_by"] = "both"
            else:
                merged_details["found_by"] = method.value
            
            merged_results.append(SearchResult(
                id=surv_id,
                name=base_result.name,
                type=base_result.type,
                score=combined_score,
                method=method,
                details=merged_details
            ))
        
        # Sort by combined score
        merged_results.sort(key=lambda x: x.score, reverse=True)
        
        return merged_results[:limit]
    
    # =========================================================================
    # MAIN ENTRY POINT - Smart Search
    # =========================================================================
    
    def smart_search(
        self,
        query: str,
        force_method: Optional[SearchMethod] = None,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Main entry point for hybrid search.
        
        1. Analyzes query to determine best method
        2. Executes appropriate search(es)
        3. Returns results with full transparency
        
        Args:
            query: User's natural language query
            force_method: Override the AI's recommendation
            limit: Max results to return
            
        Returns:
            Dict with analysis, method used, and results
        """
        
        # Step 1: Analyze query (Conditional)
        if force_method == SearchMethod.RAG:
            # OPTIMIZATION: RAG doesn't need analysis (keywords/categories), so skip the expensive Gemini call
            analysis = QueryAnalysis(
                original_query=query,
                recommended_method=SearchMethod.RAG,
                keywords=[],
                categories=[],
                biome_filter=None,
                needs_similarity_ranking=True,
                has_specific_filters=False,
                confidence=1.0,
                reasoning="Forced RAG search (Optimization: Skipped AI Analysis)"
            )
        else:
            # Keyword and Hybrid methods require extracted keywords/filters, so we must analyze
            analysis = self.analyze_query(query)
        
        # Step 2: Determine method (allow override)
        method = force_method or analysis.recommended_method
        
        # Step 3: Execute search
        if method == SearchMethod.KEYWORD:
            results = self.keyword_search(analysis, limit)
        elif method == SearchMethod.RAG:
            results = self.rag_search(query, limit)
        else:  # HYBRID
            results = self.hybrid_search(query, analysis, limit)
        
        return {
            "query": query,
            "analysis": {
                "recommended_method": analysis.recommended_method.value,
                "actual_method": method.value,
                "keywords_extracted": analysis.keywords,
                "categories": analysis.categories,
                "biome_filter": analysis.biome_filter,
                "confidence": analysis.confidence,
                "reasoning": analysis.reasoning
            },
            "results": results,
            "result_count": len(results)
        }
    
    # =========================================================================
    # SKILL SIMILARITY SEARCH (Pure RAG)
    # =========================================================================
    
    def find_similar_skills(
        self,
        skill_name: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Find skills similar to a given skill.
        
        This is a pure RAG use case - finding semantically similar items.
        Uses your existing working query pattern.
        """
        
        results = []
        
        sql = """
            WITH query_embedding AS (
                SELECT embeddings.values AS val
                FROM ML.PREDICT(
                    MODEL TextEmbeddings,
                    (SELECT @skill_name AS content)
                )
            )
            SELECT
                sk.skill_id,
                sk.name,
                sk.category,
                COSINE_DISTANCE(
                    sk.skill_embedding,
                    (SELECT val FROM query_embedding)
                ) AS distance
            FROM Skills sk
            WHERE sk.skill_embedding IS NOT NULL
              AND LOWER(sk.name) != LOWER(@skill_name)
            ORDER BY distance ASC
            LIMIT @limit
        """
        
        def run_query(transaction):
            rows = transaction.execute_sql(
                sql,
                params={
                    "skill_name": skill_name,
                    "limit": limit
                },
                param_types={
                    "skill_name": param_types.STRING,
                    "limit": param_types.INT64
                }
            )
            
            for row in rows:
                skill_id, name, category, distance = row
                results.append({
                    "skill_id": skill_id,
                    "name": name,
                    "category": category,
                    "similarity": 1 - float(distance),
                    "distance": float(distance)
                })
        
        self.database.run_in_transaction(run_query)
        
        return results
    
    # =========================================================================
    # HELPER METHODS
    # =========================================================================
    
    def _load_known_values(self):
        """Load known skills, categories, biomes from database."""
        if self._known_skills is not None:
            return
        
        def load(transaction):
            # Skills
            skill_rows = transaction.execute_sql(
                "SELECT DISTINCT name FROM Skills ORDER BY name LIMIT 100"
            )
            self._known_skills = [row[0] for row in skill_rows]
            
            # Categories
            cat_rows = transaction.execute_sql(
                "SELECT DISTINCT category FROM Skills WHERE category IS NOT NULL"
            )
            self._known_categories = [row[0] for row in cat_rows]
            
            # Biomes
            biome_rows = transaction.execute_sql(
                "SELECT DISTINCT biome FROM Survivors WHERE biome IS NOT NULL"
            )
            self._known_biomes = [row[0] for row in biome_rows]
        
        self.database.run_in_transaction(load)
    
    def _call_gemini(self, prompt: str) -> str:
        """Call Gemini model via Spanner ML.PREDICT."""
        result = None
        
        sql = """
            SELECT content
            FROM ML.PREDICT(
                MODEL GeminiPro,
                (SELECT @prompt AS prompt)
            )
        """
        
        def run_query(transaction):
            nonlocal result
            rows = transaction.execute_sql(
                sql,
                params={"prompt": prompt},
                param_types={"prompt": param_types.STRING}
            )
            for row in rows:
                result = row[0]
        
        self.database.run_in_transaction(run_query)
        
        return result or ""
