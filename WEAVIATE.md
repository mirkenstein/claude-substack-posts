# Weaviate Semantic Search

Semantic search over Substack posts and comments using Weaviate vector database with OpenAI embeddings.

## Prerequisites

- Weaviate running locally (port 8080, gRPC port 50051)
- `OPENAI_API_KEY` environment variable set
- `COHERE_API_KEY` environment variable set (for reranking)
- PostgreSQL database populated via `load_posts.py` (see [DATABASE.md](DATABASE.md))

## Quick Start

```bash
cd weaviate

# 1. Create collections
python create_collections.py

# 2. Upload posts (reads from PostgreSQL, chunks long posts, embeds via OpenAI)
python upload_posts.py

# 3. Upload comments
python upload_comments.py

# 4. Search
python search.py "Wagner mutiny Prigozhin"
python search.py --mode hybrid --collection comments "biometric surveillance"
```

## Collections

### `SubstackPostEngRu` — Posts

Posts are read from PostgreSQL (`content_html`), stripped to plain text, and chunked if longer than 5000 words. Only the `content` field is vectorized.

| Property | Type | Indexed | Description |
|----------|------|---------|-------------|
| `content` | TEXT | vectorized | Post text chunk (embedded by OpenAI) |
| `postId` | TEXT | searchable | Substack post ID |
| `title` | TEXT | searchable | Post title |
| `subtitle` | TEXT | searchable | Post subtitle |
| `slug` | TEXT | searchable | URL slug |
| `canonicalUrl` | TEXT | — | Full Substack URL |
| `postDate` | DATE | filterable | Publication date |
| `audience` | TEXT | filterable | `everyone` or `only_paid` |
| `authorName` | TEXT | filterable | Post author |
| `publicationName` | TEXT | filterable | Newsletter name |
| `subdomain` | TEXT | filterable | Substack subdomain (e.g. `esq`, `slc`) |
| `wordcount` | INT | — | Total post word count |
| `commentCount` | INT | — | Number of comments |
| `restacks` | INT | — | Number of restacks |
| `chunkNumber` | INT | — | Chunk index (0-based) |
| `totalChunks` | INT | — | Total chunks for this post (1 = not chunked) |
| `chunkTokens` | INT | — | Token count for this chunk |

### `SubstackCommentEngRu` — Comments

Comments are uploaded as-is (no chunking needed — avg ~450 chars). Only the `body` field is vectorized. Deleted comments and empty bodies are excluded.

| Property | Type | Indexed | Description |
|----------|------|---------|-------------|
| `body` | TEXT | vectorized | Comment text (embedded by OpenAI) |
| `commentId` | TEXT | searchable | Comment ID |
| `postId` | TEXT | searchable | Parent post ID |
| `postTitle` | TEXT | searchable | Parent post title (for result context) |
| `postSlug` | TEXT | — | Parent post URL slug |
| `authorName` | TEXT | filterable | Commenter name |
| `authorHandle` | TEXT | filterable | Commenter handle |
| `date` | DATE | filterable | Comment date |
| `reactionCount` | INT | — | Number of reactions |
| `depth` | INT | — | Thread depth (0 = top-level) |
| `isValuable` | BOOL | filterable | Flagged valuable (2+ links, 10+ reactions, or long with links) |
| `subdomain` | TEXT | filterable | Source publication subdomain |

### Embedding & Reranking

Both collections use:

| Component | Model | Details |
|-----------|-------|---------|
| Vectorizer | `text-embedding-3-small` | 1536 dimensions, OpenAI |
| Reranker | `rerank-english-v3.0` | Cohere |

## Chunking Strategy

Posts over 5000 words are split into token-based chunks for better embedding quality:

| Setting | Value | Rationale |
|---------|-------|-----------|
| Chunk size | 700 tokens | Larger than transcripts due to denser content |
| Overlap | 150 tokens | Context preservation across chunk boundaries |
| Min chunk | 400 tokens | Small remainders merge into the previous chunk |
| Tokenizer | `cl100k_base` | Same tokenizer as `text-embedding-3-small` |
| Threshold | 5000 words | Posts at or below this are a single object |

Posts under the threshold are stored as a single object (`chunkNumber=0`, `totalChunks=1`).

## HTML Stripping

Post content is stored as HTML in PostgreSQL. The upload script strips HTML to plain text using Python's `html.parser`:

- Extracts text content, skipping `<script>` and `<style>` tags
- Inserts newlines at block elements (`<p>`, `<div>`, `<h1>`–`<h4>`, `<li>`, `<blockquote>`, `<br>`)
- Collapses excessive whitespace while preserving paragraph breaks

## Search CLI

```
python search.py [QUERY] [OPTIONS]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `QUERY` | `"Wagner mutiny Prigozhin"` | Search query text |
| `--limit N` | `5` | Number of results |
| `--collection` | `both` | `posts`, `comments`, or `both` |
| `--mode` | `semantic` | `semantic`, `hybrid`, or `rerank` |
| `--alpha` | `0.5` | Hybrid balance: 0.0 = pure keyword, 1.0 = pure semantic |

### Search Modes

**Semantic** (`near_text`) — finds results by meaning, even without keyword overlap:
```bash
python search.py "consequences of Western sanctions on Russia"
```

**Hybrid** — combines semantic similarity with BM25 keyword matching:
```bash
python search.py --mode hybrid --alpha 0.7 "Prigozhin Wagner"
```

**Reranked** — semantic search + Cohere reranking for precision:
```bash
python search.py --mode rerank "digital surveillance biometrics"
```

## Deterministic UUIDs

Both upload scripts use `uuid5` for idempotent uploads — re-running overwrites rather than duplicates:

- Posts: `uuid5(NAMESPACE_DNS, "engru-post-{postId}-{chunkNumber}")`
- Comments: `uuid5(NAMESPACE_DNS, "engru-comment-{commentId}")`

## Verification

After uploading, verify counts:

```python
from config import get_client, POSTS_COLLECTION, COMMENTS_COLLECTION

client = get_client()
posts = client.collections.get(POSTS_COLLECTION)
comments = client.collections.get(COMMENTS_COLLECTION)

print(posts.aggregate.over_all(total_count=True).total_count)
print(comments.aggregate.over_all(total_count=True).total_count)
client.close()
```

## File Structure

```
weaviate/
  config.py              # Connection helper, collection names, chunking constants
  create_collections.py  # Creates both collections with vectorizer + reranker config
  upload_posts.py        # PostgreSQL → HTML strip → chunk → Weaviate batch upload
  upload_comments.py     # PostgreSQL → Weaviate batch upload (no chunking)
  search.py              # CLI for semantic, hybrid, and reranked search
```
