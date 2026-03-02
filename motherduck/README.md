# MotherDuck Upload Scripts

Upload pipeline from PostgreSQL to [MotherDuck](https://motherduck.com/) (SaaS DuckDB) for semantic search and catalog browsing via Claude Desktop MCP.

## Setup

Requires `MOTHERDUCK_TOKEN` environment variable. Token stored in `.env` (not committed). All scripts connect to `md:my_db`.

```bash
export MOTHERDUCK_TOKEN='...'
pip install duckdb tiktoken
```

## Tables

| Table | Rows | Source | Description |
|-------|------|--------|-------------|
| `substack_posts` | ~8,475 chunks | substack + podcasts DBs | Chunked blog post content |
| `substack_comments` | ~83,000 | substack + podcasts DBs | Individual comments |
| `youtube_videos` | ~281 | podcasts DB | One row per video (catalog) |
| `youtube_chapters` | ~3,400 | podcasts DB | One row per chapter (chapter-aware) |
| `youtube_transcript_chunks` | ~19,700 | podcasts DB | Fixed-window transcript chunks |

## Scripts

### upload_posts.py

Chunked Substack posts with token-window chunking (700 tokens, 150 overlap). Default incremental (dedup by post_id), `--full` for drop+recreate.

```bash
python motherduck/upload_posts.py                           # incremental from both DBs
python motherduck/upload_posts.py --database substack       # substack DB only
python motherduck/upload_posts.py --publication edwardslavsquat  # one publication
python motherduck/upload_posts.py --full                    # drop and recreate
python motherduck/upload_posts.py --skip-embeddings         # insert only
python motherduck/upload_posts.py --embed-only              # only run embeddings
```

**Key columns:** `chunk_id` (BIGINT, `{post_id}{chunk_number:02d}`), `content`, `content_embedding FLOAT[512]`

**FTS index** on `content`, `title`, `subtitle` keyed by `chunk_id`.

### upload_comments.py

Individual Substack comments. Default incremental (dedup by comment_id), `--full` for drop+recreate.

```bash
python motherduck/upload_comments.py                           # incremental from both DBs
python motherduck/upload_comments.py --database substack       # substack DB only
python motherduck/upload_comments.py --publication edwardslavsquat  # one publication
python motherduck/upload_comments.py --full                    # drop and recreate
python motherduck/upload_comments.py --skip-embeddings
python motherduck/upload_comments.py --embed-only
```

**Key columns:** `comment_id` (VARCHAR, numeric), `body`, `body_embedding FLOAT[512]`

**FTS index** on `body` keyed by `comment_id`.

### upload_video_chapters.py

Two-table design for YouTube video catalog and chapter-aware transcript chunks. Mirrors `weaviate/upload_videos_chapters.py`. Full reload each run (CREATE OR REPLACE).

```bash
python motherduck/upload_video_chapters.py                         # full reload
python motherduck/upload_video_chapters.py --database podcasts     # explicit DB
python motherduck/upload_video_chapters.py --skip-embeddings
python motherduck/upload_video_chapters.py --embed-only
```

**`youtube_videos`**: one row per video. Embeds `title || description`. FTS on `title`, `description`, `channel_name`, `playlist_name`.

**`youtube_chapters`**: one row per chapter with grouped transcript segments. Videos without chapters get a single "Full Transcript" row. FTS on `chapter_title`, `transcript`.

### upload_video_transcripts.py

Fixed-window transcript chunks (1000 tokens, 250 overlap). Mirrors `weaviate/upload_videos.py`. Full reload each run.

```bash
python motherduck/upload_video_transcripts.py                         # full reload
python motherduck/upload_video_transcripts.py --database podcasts     # explicit DB
python motherduck/upload_video_transcripts.py --skip-embeddings
python motherduck/upload_video_transcripts.py --embed-only
```

**Key columns:** `row_id` (BIGINT, sequential), `video_id`, `transcript`, `transcript_embedding FLOAT[512]`

**FTS index** on `transcript`, `video_title` keyed by `row_id`.

## Embeddings & Search

All tables use MotherDuck's built-in `embedding()` function (OpenAI `text-embedding-3-small`, 512 dimensions). Embeddings are generated server-side after insert.

**Semantic search:**
```sql
SELECT title, subdomain,
       array_cosine_similarity(embedding('Ukraine war'), content_embedding) AS score
FROM substack_posts
ORDER BY score DESC
LIMIT 10
```

**Full-text search (BM25):** Requires `LOAD fts` in each session.
```sql
LOAD fts;
SELECT chunk_id, title, fts_main_substack_posts.match_bm25(chunk_id, 'Ukraine war') AS score
FROM substack_posts
WHERE score IS NOT NULL
ORDER BY score DESC
LIMIT 10
```

**FTS key constraint:** DuckDB FTS `match_bm25()` requires the document key to be castable to integer. All tables use BIGINT keys (`chunk_id`, `comment_id`, `row_id`).

## Chapter-Aware vs Fixed-Window Transcripts

Both `youtube_chapters` and `youtube_transcript_chunks` contain video transcripts but chunked differently:

- **`youtube_chapters`** — chunks follow chapter boundaries from video descriptions. Better for browsing and structured queries ("show me chapters about topic X").
- **`youtube_transcript_chunks`** — fixed 1000-token windows with 250-token overlap. Better for uniform semantic search coverage across all videos (including those without chapters).
