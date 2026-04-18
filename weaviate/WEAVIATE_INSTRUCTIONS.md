# Weaviate Search — Claude Project Instructions

> **Purpose:** Generic reference for working with Weaviate vector database collections via the `weaviate-nouvx:` MCP tools. Import this into any Claude Project that connects to Weaviate.

---

## Available Tools

| Tool | Purpose |
|---|---|
| `list_collections` | List all collections in the database |
| `get_schema` | Get collection schema (properties, vectorizer, reranker config) |
| `get_collection_objects` | Browse/paginate collection contents (no query needed) |
| `keyword_search` | BM25 keyword search |
| `semantic_search` | Vector similarity search |
| `hybrid_search` | Combined BM25 + vector search (Reciprocal Rank Fusion) |
| `search` | Alias for hybrid search (default alpha=0.3) |
| `check_connection` | Verify Weaviate is reachable |
| `get_config` | Show connection config |
| `is_multi_tenancy_enabled` | Check if collection uses multi-tenancy |
| `get_tenant_list` | List tenants for multi-tenant collections |

---

## Discovery Workflow

When starting work with an unfamiliar Weaviate instance, follow this sequence:

```
1. list_collections          → See what's available
2. get_schema(collection)    → Learn properties, vectorizer, reranker
3. get_collection_objects     → Sample some data to understand content
4. keyword_search / search    → Start querying
```

The schema call is essential — it tells you which field is vectorized (look for the `description` on properties, e.g., "Chunk content (vectorized)"), what reranker is available, and whether the collection supports multi-tenancy.

---

## Search Modes

All search tools accept optional `where_filter` and `rerank` parameters.

### 1. Keyword Search (`keyword_search`)

**Algorithm:** BM25 (term frequency / inverse document frequency)
**Best for:** Exact names, specific terms, direct mentions, known phrases.
**Returns:** `score` (BM25 relevance — higher is better)

```
keyword_search(
    collection_name="MyCollection",
    query="Jeffrey Epstein blackmail",
    limit=10
)
```

**Optional: `query_properties`** — restrict which text fields BM25 searches. Supports boost weights.
```
query_properties=["body"]                    # search only body
query_properties=["body^10", "title"]        # boost body 10x over title
```

### 2. Semantic Search (`semantic_search`)

**Algorithm:** Vector cosine similarity — the query text is embedded on the fly and compared to stored vectors.
**Best for:** Conceptual/thematic queries, finding related content without exact terms.
**Returns:** `distance` (vector distance — lower is more similar)

```
semantic_search(
    collection_name="MyCollection",
    query="intelligence agency blackmail operations",
    limit=10
)
```

This is the key advantage over keyword search: you can describe a concept and find content that discusses it even if none of your exact words appear in the text.

### 3. Hybrid Search (`hybrid_search` or `search`)

**Algorithm:** Reciprocal Rank Fusion combining BM25 + vector search.
**Best for:** General-purpose queries where you want both exact matches and semantic relevance.
**Returns:** `score` (combined relevance)

```
hybrid_search(
    collection_name="MyCollection",
    query="Epstein Harvard funding",
    alpha=0.5,
    limit=10
)
```

**`alpha` parameter** controls the balance:
- `1.0` = pure vector (100% semantic)
- `0.0` = pure BM25 (100% keyword)
- `0.5` = equal weight
- `0.3` = default (30% vector, 70% keyword)

The `search` tool is an alias for `hybrid_search` with `alpha=0.3`.

### Recommended Strategy

| Scenario | Tool | Notes |
|---|---|---|
| Exact name/term lookup | `keyword_search` | Best precision for specific terms |
| Conceptual/thematic exploration | `semantic_search` | Finds related content without exact terms |
| General purpose (balanced) | `hybrid_search` or `search` | Good default for most queries |
| Broad search → narrow focus | Any + `rerank` | Use a more specific rerank query |
| Scoped to metadata | Any + `where_filter` | Filter by date, author, source, etc. |
| Maximum precision | Any + `where_filter` + `rerank` | Combine both for best results |
| Browse without a query | `get_collection_objects` | Supports `where_filter` for scoped browsing |

For research tasks, a multi-pass approach often works best:
1. Start with **keyword search** for exact terms
2. Follow up with **semantic search** for thematic coverage
3. Use **hybrid search** as a balanced single-pass alternative

---

## Filtering with `where_filter`

The `where_filter` parameter narrows results by metadata **before** search scoring. Available on all search tools and on `get_collection_objects`.

### Filter Operators

| Operator | Works On | Example |
|---|---|---|
| `Equal` | text, int | `{"property": "author", "operator": "Equal", "value": "Whitney Webb"}` |
| `NotEqual` | text, int | `{"property": "audience", "operator": "NotEqual", "value": "everyone"}` |
| `GreaterThan` | int, date | `{"property": "uploadDate", "operator": "GreaterThan", "value": "2025-01-01T00:00:00Z"}` |
| `GreaterThanEqual` | int, date | `{"property": "chunkNumber", "operator": "GreaterThanEqual", "value": 5}` |
| `LessThan` | int, date | `{"property": "uploadDate", "operator": "LessThan", "value": "2025-12-31T00:00:00Z"}` |
| `LessThanEqual` | int, date | `{"property": "chunkTokens", "operator": "LessThanEqual", "value": 500}` |
| `Like` | text | `{"property": "title", "operator": "Like", "value": "*Epstein*"}` |
| `ContainsAny` | text[] | For array properties |
| `ContainsAll` | text[] | For array properties |
| `IsNone` | any | Check if property is null |

**Date format:** Always use ISO 8601 with timezone: `"2025-01-01T00:00:00Z"`

**Wildcard matching:** The `Like` operator uses `*` as wildcard (not `%`).

### Compound Filters (And / Or)

```json
// AND — both conditions must match
{
  "operator": "And",
  "operands": [
    {"property": "uploadDate", "operator": "GreaterThan", "value": "2025-01-01T00:00:00Z"},
    {"property": "uploadDate", "operator": "LessThan", "value": "2025-12-31T00:00:00Z"}
  ]
}

// OR — either condition matches
{
  "operator": "Or",
  "operands": [
    {"property": "author", "operator": "Equal", "value": "Whitney Webb"},
    {"property": "author", "operator": "Equal", "value": "Mark Goodwin"}
  ]
}
```

### Common Filter Patterns

```python
# Date range (all of 2025)
where_filter={"operator": "And", "operands": [
    {"property": "uploadDate", "operator": "GreaterThan", "value": "2025-01-01T00:00:00Z"},
    {"property": "uploadDate", "operator": "LessThan", "value": "2026-01-01T00:00:00Z"}
]}

# First chunk only (intros/summaries — useful for browsing catalogs)
where_filter={"property": "chunkNumber", "operator": "Equal", "value": 0}

# Wildcard title match
where_filter={"property": "title", "operator": "Like", "value": "*Epstein*"}

# Exclude a specific value
where_filter={"property": "author", "operator": "NotEqual", "value": "Around the Web"}

# Isolate a single item by ID
where_filter={"property": "videoId", "operator": "Equal", "value": "BvLz1bI2sXU"}
```

---

## Reranking with `rerank`

Reranking is a **second-pass relevance scoring** applied after the initial search. The Weaviate server runs a reranker model (check `get_schema` for which model — commonly `jina-reranker-v3` or `rerank-english-v3.0`) that re-scores each result against a query, producing a `rerank_score` alongside the original `score` or `distance`. Results are reordered by rerank score.

### When to Use Rerank

- **Broad initial query + specific focus:** Search broadly then rerank toward a specific angle
- **Keyword search refinement:** BM25 finds all mentions of a term; reranker promotes the most topically relevant ones
- **Cross-topic disambiguation:** When a name/term appears in many unrelated contexts, rerank focuses results

### Rerank Parameters

```json
// Basic — reranks using the original search query against a specified text property
{"property": "transcript"}

// Custom query — reranks using a DIFFERENT query than the search query
{"property": "transcript", "query": "Harvard science funding Epstein donations"}
```

- **`property`** (required) — The text field to rerank against. Must be a text property in the collection. Use whatever field holds the main content (e.g., `transcript`, `content`, `email_text`).
- **`query`** (optional) — Override rerank query. If omitted, uses the original search query. **The power move is using a different, more specific rerank query than your broad search query.**

### Rerank Examples

```python
# Broad keyword search, reranked toward a specific subtopic
keyword_search(
    collection_name="VideoChunkPodcasts",
    query="Epstein",
    limit=10,
    rerank={"property": "transcript", "query": "Epstein Harvard science funding Martin Nowak"}
)

# Semantic search with rerank to refine
semantic_search(
    collection_name="SubstackPostPodcasts",
    query="blackmail operation intelligence",
    limit=10,
    rerank={"property": "content", "query": "Dershowitz massage accusation"}
)

# Hybrid search + rerank for maximum precision
hybrid_search(
    collection_name="VideoChunkPodcasts",
    query="9/11 controlled demolition",
    alpha=0.5,
    limit=10,
    rerank={"property": "transcript", "query": "Building 7 free fall collapse"}
)
```

### Rerank Score Interpretation

- **Positive scores** (0.1+) = strong match to the rerank query
- **Scores near zero** = marginal relevance
- **Negative scores** = poor match (demoted)
- Results are sorted by `rerank_score` descending when rerank is active

### Combining Filter + Rerank

Filters and rerank can be used together for maximum precision:

```python
keyword_search(
    collection_name="VideoChunkPodcasts",
    query="Epstein",
    limit=10,
    where_filter={"operator": "And", "operands": [
        {"property": "uploadDate", "operator": "GreaterThan", "value": "2025-01-01T00:00:00Z"},
        {"property": "uploadDate", "operator": "LessThan", "value": "2026-01-01T00:00:00Z"}
    ]},
    rerank={"property": "transcript", "query": "Maxwell sisters Mossad connection"}
)
```

---

## Browsing Collections

Use `get_collection_objects` to browse collection contents without a search query. Supports pagination and filtering.

```python
# Browse first 10 objects
get_collection_objects(collection_name="MyCollection", limit=10)

# Browse with offset for pagination
get_collection_objects(collection_name="MyCollection", limit=10, offset=10)

# Browse filtered subset (e.g., first chunks only)
get_collection_objects(
    collection_name="MyCollection",
    limit=20,
    where_filter={"property": "chunkNumber", "operator": "Equal", "value": 0}
)
```

---

## Working with Chunked Content

Most collections store content as multiple chunks per source document (video, article, post). Key patterns:

### Understand chunk structure
Every chunk typically has:
- An **ID field** linking chunks to their source (e.g., `videoId`, `postId`, `articleId`)
- **`chunkNumber`** — zero-indexed position within the source
- **`totalChunks`** — how many chunks the source was split into
- **`chunkTokens`** — token count for this chunk

### Get all chunks for a specific source
Use `where_filter` on the ID field and search with a broad or specific query:
```python
keyword_search(
    collection_name="MyCollection",
    query="topic of interest",
    limit=50,
    where_filter={"property": "videoId", "operator": "Equal", "value": "BvLz1bI2sXU"}
)
```

Or browse all chunks without a query:
```python
get_collection_objects(
    collection_name="MyCollection",
    limit=50,
    where_filter={"property": "videoId", "operator": "Equal", "value": "BvLz1bI2sXU"}
)
```

### Browse catalog (first chunks only)
First chunks (`chunkNumber = 0`) typically contain the opening/introduction and serve as a catalog:
```python
get_collection_objects(
    collection_name="MyCollection",
    limit=20,
    where_filter={"property": "chunkNumber", "operator": "Equal", "value": 0}
)
```

### Cross-collection research
When researching a topic across multiple collections, search all relevant collections and synthesize:
1. Search each collection with the same query
2. Use keyword search for exact terms, semantic for thematic coverage
3. Cross-reference findings by looking for shared names, dates, or concepts

---

## Tips and Gotchas

### Search query tips
- **Keep queries short and specific** — 2-6 words work best
- **Use keyword search for names** — BM25 excels at exact term matching
- **Use semantic search for concepts** — "intelligence agency financial control" finds content about CIA/banking even without those exact words
- **Rerank is your precision tool** — broad search + specific rerank query is often better than a narrow initial search

### Common mistakes to avoid
- **Don't use SQL syntax** — Weaviate is not a database; use the search tools and filters
- **Don't forget the `T00:00:00Z` on dates** — date filters require full ISO 8601 format
- **Don't use `%` for wildcards** — use `*` with the `Like` operator
- **Don't assume property names** — always check `get_schema` first; properties use camelCase (e.g., `videoId` not `video_id`, `uploadDate` not `upload_date`)
- **Don't search with huge limits unnecessarily** — start with `limit=5` or `limit=10` and increase if needed

### Understanding results
- Search results return objects with their `id`, `collection`, `properties`, and relevance score
- The `properties` dict contains all the metadata fields for each result
- For keyword search: higher `score` = more relevant
- For semantic search: lower `distance` = more similar
- For hybrid/reranked: results are sorted by the combined or rerank score

### Multi-tenant collections
Some collections use multi-tenancy for data isolation. If `is_multi_tenancy_enabled` returns true:
- You must provide `tenant_id` on all search and browse calls
- Use `get_tenant_list` to discover available tenants
- Each tenant's data is isolated from other tenants
