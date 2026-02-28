# Embedded Vector Database Experiments

Experimental alternatives to the primary Weaviate Docker setup. All three use **Jina AI `jina-embeddings-v3`** (1024 dimensions) for embeddings, tested with the drlivci publication (39 posts, 332 chunks).

## Comparison

| | Weaviate Docker (primary) | Weaviate Embedded | LanceDB | ChromaDB |
|---|---|---|---|---|
| **Location** | `weaviate/` | `weaviate/embedded.py` | `embedded_vectordb/lancedb/` | `embedded_vectordb/chromadb/` |
| **Server needed** | Docker container | In-process (background script) | No | No |
| **Embeddings** | OpenAI `text-embedding-3-small` (1536d) | Jina AI `jina-embeddings-v3` (1024d) | Jina AI `jina-embeddings-v3` (1024d) | Jina AI `jina-embeddings-v3` (1024d) |
| **Reranker** | Cohere `rerank-english-v3.0` | Jina `jina-reranker-v2-base-multilingual` | N/A | N/A |
| **Upload speed** | 26 chunks/sec | 26 chunks/sec | 47 chunks/sec | 39 chunks/sec |
| **Search modes** | semantic, hybrid, rerank | semantic, hybrid, rerank | vector, FTS, hybrid | semantic |
| **Data path** | Docker volume | `~/.local/share/weaviate-embedded/` | `~/.local/share/lancedb-substack/` | `~/.local/share/chromadb-substack/` |
| **Ports** | 8080 / 50051 | 8079 / 50060 | N/A | N/A |
| **Python 3.14** | OK | OK | OK | Needs patched build (pydantic v1 incompatibility) |

## 1. Weaviate Embedded (Jina AI)

Lives in `weaviate/embedded.py` alongside the main Weaviate scripts. Uses `WEAVIATE_EMBEDDED=1` env var to switch all weaviate scripts between Docker and embedded mode.

```bash
# Start embedded instance (runs in foreground)
JINAAI_API_KEY=... python weaviate/embedded.py

# In another terminal, use the same scripts as Docker but with env var
WEAVIATE_EMBEDDED=1 JINAAI_API_KEY=... python weaviate/create_collections.py
WEAVIATE_EMBEDDED=1 JINAAI_API_KEY=... python weaviate/upload_posts.py --publication drlivci
WEAVIATE_EMBEDDED=1 JINAAI_API_KEY=... python weaviate/search.py "Wagner mutiny"
```

## 2. LanceDB (Jina AI)

Fully embedded — no server process at all. Stores data as Lance columnar files. Supports vector search, full-text search (with `create_fts_index`), and hybrid search.

```bash
JINA_API_KEY=... python embedded_vectordb/lancedb/create_tables.py
JINA_API_KEY=... python embedded_vectordb/lancedb/upload_posts.py --publication drlivci
JINA_API_KEY=... python embedded_vectordb/lancedb/search.py "Chubais privatization"
JINA_API_KEY=... python embedded_vectordb/lancedb/search.py "Wagner mutiny" --mode fts
```

Note: FTS requires creating an index first (done automatically on first upload, or manually via `table.create_fts_index('content')`).

## 3. ChromaDB (Jina AI)

Persistent embedded client. Simplest API of the three. Required a manual patch to `chromadb/config.py` for Python 3.14 compatibility (pydantic v1 → pydantic-settings migration). See [chroma-core/chroma#5996](https://github.com/chroma-core/chroma/issues/5996).

```bash
JINA_API_KEY=... python embedded_vectordb/chromadb/create_collections.py
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_posts.py --publication drlivci
JINA_API_KEY=... python embedded_vectordb/chromadb/search.py "Wagner mutiny"
```

### ChromaDB Python 3.14 Fix

ChromaDB's `Settings` class uses pydantic v1's `BaseSettings` which breaks on Python 3.14 (`chroma_server_nofile` type inference failure). See [chroma-core/chroma#5996](https://github.com/chroma-core/chroma/issues/5996).

**Permanent fix**: A local clone of chromadb at `~/PycharmProjects/chroma/` has the fix (commit `4da7add31` — drops pydantic v1 compat layer, migrates to pydantic v2 `field_validator` + `pydantic-settings`). This PR is pending merge upstream.

### Local chroma-mcp venv (MCP server)

The `chroma-mcp` MCP server bundles upstream chromadb which breaks on Python 3.14. A dedicated venv at `~/chroma-mcp-local/` uses the patched chromadb build:

```bash
# One-time setup (already done)
python3.14 -m venv ~/chroma-mcp-local
~/chroma-mcp-local/bin/pip install ~/PycharmProjects/chroma/    # patched chromadb
~/chroma-mcp-local/bin/pip install chroma-mcp --no-deps         # MCP server without overwriting chromadb
~/chroma-mcp-local/bin/pip install mcp posthog backoff httpx-sse python-multipart starlette sse-starlette  # runtime deps
```

MCP server config (replace `uvx` command):
```json
{
  "command": "/home/mnm/chroma-mcp-local/bin/chroma-mcp",
  "args": ["--chroma-path", "/home/mnm/.local/share/chromadb-substack"]
}
```

Once the chromadb PR is merged and released to PyPI, the chroma-mcp package just needs a version pin bump and `uvx` will work again natively.

### Legacy manual patch (deprecated)

Previously applied directly to `.venv/lib64/python3.14/site-packages/chromadb/config.py`:
1. Import `BaseSettings` from `pydantic_settings` (install: `pip install pydantic-settings`)
2. Move `chroma_server_nofile` field before its `@validator` decorator
3. Add type annotations to `chroma_coordinator_host: str`, `chroma_logservice_host: str`, `chroma_logservice_port: int`
4. Add `extra = "allow"` to the inner `Config` class

This breaks on every `pip install` or cache clear. Use the local venv approach above instead.

## Environment Variables

| Variable | Used by |
|---|---|
| `JINAAI_API_KEY` | Weaviate embedded (module-level key) |
| `JINA_API_KEY` | LanceDB and ChromaDB (Jina client key) |
| `WEAVIATE_EMBEDDED=1` | Switches weaviate scripts to embedded mode |

Note: Jina uses two different env var names — Weaviate's module expects `JINAAI_API_KEY`, while LanceDB and ChromaDB's Jina clients expect `JINA_API_KEY`. Both use the same API key value.
