# CLAUDE.md

## Project Overview

Substack newsletter archival pipeline: fetch posts/comments/podcasts, store in PostgreSQL, index in Weaviate for semantic search, transcribe podcast episodes.

## Directory Structure

```
posts/{subdomain}/                  # Post JSON files + post list
posts/{subdomain}/{subdomain}_posts.json  # Post metadata list
posts/saved/images/{post_id}/       # Downloaded post images
posts/saved/audio/{post_id}/        # Downloaded podcast audio
transcribe/                         # Transcription scripts and outputs
weaviate/                           # Weaviate vector search (primary)
weaviate/embedded.py                # Experimental: embedded Weaviate with Jina AI
embedded_vectordb/                  # Experimental: LanceDB and ChromaDB with Jina AI
motherduck/                         # MotherDuck SaaS DuckDB (experimental)
twitter/                            # Twitter data ingestion (separate pipeline)
src/                                # Core library code
src/db/                             # Database connection and loader
```

## Key Scripts and Their Roles

### Ingestion Pipeline

| Script | Purpose |
|--------|---------|
| `main.py` | Fetch post lists (`--list --all`) and full post content (`--from-list`). `--last N` re-fetches most recent N posts |
| `load_posts.py` | Load post JSONs into PostgreSQL. `--resume`, `--log`, `--last N` (reload N most recently modified files, bypasses resume) |
| `weaviate/upload_posts.py` | Upload posts to Weaviate. Watermark-based incremental uploads (`--publication`) |
| `refresh_blog.py` | End-to-end incremental refresh: diff, fetch, load, upload. Convenience wrapper |
| `download_media.py` | Extract and download images/audio from posts |
| `analyze_media.py` | Two-pass LLM image analysis (see Media Analysis section below) |
| `ingest_transcripts.py` | Load podcast transcripts into `transcript_lines` table and Weaviate |

### load_posts.py vs refresh_blog.py postgres loading

Both load posts into PostgreSQL using `PostLoader`, but `load_posts.py` is the canonical loader:

- **`load_posts.py`**: Full-featured ETL with `--resume` (checks `load_status` table), structured logging to file (`--log`), error recording in DB, graceful JSON read error handling. Use for bulk loads and recovery.
- **`refresh_blog.py`**: Has an inline `load_into_postgres()` that works for the happy path (1-2 new posts) but lacks resume, file logging, and error recording in DB. It calls `weaviate/upload_posts.py` externally but reimplements the postgres loading inline.

When in doubt, prefer `load_posts.py --resume` for postgres loading.

### Transcription (`transcribe/`)

| Script | Purpose |
|--------|---------|
| `transcribe_all.sh` | Batch transcription with per-episode speaker count inference from DB heuristics |
| `transcribe/transcribe_interview.py` | Single episode transcription using WhisperX + pyannote diarization |
| `transcribe/SETUP.md` | One-time setup: dependencies, pyannote license acceptance, HF token |
| `transcribe/TRANSCRIPT_INGESTION_PLAN.md` | Design doc for transcript ingestion pipeline |
| `ingest_transcripts.py` | Load transcripts into `transcript_lines` table, update `posts.content_html`, upload to Weaviate |

```bash
# Batch transcribe all untranscribed episodes for a publication
./transcribe_all.sh --subdomain slavlandchronicles
./transcribe_all.sh --subdomain martyrmade --database podcasts

# Dry run (preview what would be transcribed with speaker counts)
./transcribe_all.sh --subdomain slavlandchronicles --dry-run

# Force speaker count override
./transcribe_all.sh --subdomain slavlandchronicles --num-speakers 2

# Single episode
python transcribe/transcribe_interview.py posts/saved/audio/anti-empire/84809249/episode.mp3 \
    --num-speakers 3 --model large-v3 --hf-token "$HUGGING_FACE_HUB_TOKEN"

# Ingest transcripts into Postgres + Weaviate
python ingest_transcripts.py posts/saved/audio/anti-empire/
python ingest_transcripts.py posts/saved/audio/anti-empire/83709910  # single episode
python ingest_transcripts.py posts/saved/audio/martyrmade/ --database podcasts
python ingest_transcripts.py posts/saved/audio/martyrmade/ --database podcasts --skip-weaviate
python ingest_transcripts.py posts/saved/audio/martyrmade/ --database podcasts --skip-content-html
```

Requires `HUGGING_FACE_HUB_TOKEN` environment variable. Default Whisper model: `large-v3`.

Speaker count heuristics in `transcribe_all.sh`:
- **anti-empire**: Always 3 speakers (WOAW interview format: Marco, Rolo, Slavsquat)
- **edwardslavsquat**: Checks description for multiple names, "w/", "conversation with", "interview with" patterns
- **slavlandchronicles**: Checks title for `w/` or `W/` pattern, WOAW episodes get 3
- **martyrmade**: Checks title for `w/` (excluding `w/audio`), comma/and patterns for multiple guests
- Default: 1 speaker (solo podcast)

### External Sources

`external_sources/load_external.py` loads scraped articles from non-Substack sites into the `external` schema. Sources: TopWar, LiveJournal, Katyusha, WSJ, Washington Post, TopCor, The Nation, Liberium, Versia, Paul Craig Roberts, Iurie Roșca (arcaluinoe.info).

```bash
python external_sources/load_external.py                                        # load all JSON files from scraped_data/
python external_sources/load_external.py --file topcor_articles.json            # load one file
python external_sources/load_external.py --file new_cumulative/topwar_articles.json  # load from subdirectory
python external_sources/load_external.py --recreate                             # drop & recreate schema, then load all
python external_sources/load_external.py --recreate --no-load                   # just recreate schema
python external_sources/load_external.py --database podcasts                    # load into a different database
```

- Schema defined in `external_sources/create_external_tables.sql`
- Source domain is inferred from filename prefix via `FILENAME_DOMAIN_MAP` (e.g. `topwar_*` / `top_war_*` -> `topwar.ru`, `WSJ_*` -> `wsj.com`)
- New sources are auto-created if not in the seed list
- **Articles**: upsert on `(source_id, url)` using `COALESCE` — existing data is preserved, only NULLs are filled
- **Comments**: upsert on `(article_id, content_hash)` where `content_hash = md5(username || body)` — stable IDs across re-runs
- JSON files go in `external_sources/scraped_data/`
- `external_sources/scraped_data/new_cumulative/` — latest cumulative scrape files (all sources, superset of original files)
- `--recreate` runs `DROP SCHEMA external CASCADE` then re-runs the SQL file — use when schema changes

**Weaviate upload** (`weaviate/upload_external.py`) supports watermark-based incremental uploads:

```bash
python weaviate/upload_external.py                    # incremental (new/updated since last run)
python weaviate/upload_external.py --all              # re-upload everything
python weaviate/upload_external.py --since 2025-01-01 # upload records updated after date
python weaviate/upload_external.py --articles-only    # only articles
python weaviate/upload_external.py --comments-only    # only comments
python weaviate/upload_external.py --database podcasts # upload to ExternalArticlePodcasts/ExternalCommentPodcasts
```

### Media Download and Analysis

**Download** (`download_media.py`) extracts image/audio URLs from posts and downloads them locally:

```bash
python download_media.py --extract                    # populate post_media table from HTML
python download_media.py --download                   # download all undownloaded media
python download_media.py --download --type image      # images only
python download_media.py --download --type cover_image
python download_media.py --download --type audio
python download_media.py --extract --download         # both steps
```

**Analysis** (`analyze_media.py`) uses a two-pass LLM workflow on downloaded images:

- **Pass 1** — Fast triage with local Ollama (`gemma3:27b`). Produces description, category, and basic data extraction for every image.
- **Pass 2** — Deep analysis with Anthropic Haiku on "critical" categories only: `screenshot`, `tweet`, `document`, `chart`, `table`, `infographic`, `map`. Includes pass-1 results as context.

```bash
# Pass 1: run on all unanalyzed images (default model: ollama/gemma3:27b)
PYTHONUNBUFFERED=1 nohup python analyze_media.py > analyze_pass1.log 2>&1 &

# Monitor progress
tail -f analyze_pass1.log

# Pass 2: deep analysis on critical images with Haiku
python analyze_media.py --pass2 --api-key 'sk-ant-...'

# Other options
python analyze_media.py --model ollama/qwen2.5vl:32b  # different ollama model
python analyze_media.py --publication kk               # one publication only
python analyze_media.py --limit 50                     # test run
python analyze_media.py --reanalyze                    # redo already-analyzed
python analyze_media.py --type cover_image             # cover images only
```

How it works:
- Reads from `post_media` where `downloaded_at IS NOT NULL` and `analyzed_at IS NULL` (resume-safe)
- Pass 2 filters: `image_category IN (critical set)` AND `analysis_model NOT LIKE 'claude%'`
- Saves JSON analysis alongside each image: `{image}.analysis.json`
- Updates DB columns: `image_description`, `image_data` (JSONB), `image_category`, `analysis_model`, `analyzed_at`
- `image_data` JSONB contains: `extracted_text`, `entities`, `chart_description`, `chart_data`, `source`, `date_depicted`
- After pass 1, prints category breakdown showing which categories are critical for pass 2

Key files:
- `analyze_media.py` — CLI orchestrator
- `src/image_analyzer.py` — LLM vision backends (Anthropic, Ollama), prompt templates, JSON parsing, magic-bytes MIME detection

### Chapter-Aware Video Chunking

`weaviate/upload_videos_chapters.py` uploads YouTube transcript segments to Weaviate using chapter boundaries instead of fixed token windows. Dedicated collection `VideoChapterChunkPodcasts`, separate from the fixed-window `VideoChunkPodcasts`.

```bash
python weaviate/upload_videos_chapters.py --database podcasts         # incremental
python weaviate/upload_videos_chapters.py --database podcasts --all   # re-upload all
python weaviate/upload_videos_chapters.py --database podcasts --upload-only
python weaviate/upload_videos_chapters.py --database podcasts --create-only
python weaviate/upload_videos_chapters.py --database podcasts --since '2026-02-28'
```

**Data flow**: Reads from `youtube.transcript_segments` (per-segment timestamps) and `youtube.video_chapters` (chapter markers). Groups segments by chapter boundary, sub-chunks large chapters (>1000 tokens) with overlap, merges small chapters (<500 tokens) forward. Videos without chapters fall back to fixed-window chunking with `chunkMethod="fixed_window"`.

**Chapter bootstrap**: The script parses chapter timestamps (`H:MM:SS` / `MM:SS` patterns) from `youtube.video_transcripts.description` and populates `youtube.video_chapters` on startup (ON CONFLICT DO NOTHING). This is an exception to the normal pattern where PostgreSQL is populated by separate ETL scripts and Weaviate upload scripts only read from it. The chapter data originates from video descriptions already in PG — the script just extracts structured data from unstructured text.

**Chunk properties**: `transcript` (vectorized), `description`, `videoId`, `videoTitle`, `channelName`, `videoUrl`, `uploadDate`, `playlistName`, `chapterTitle`, `chapterNumber`, `chapterStartTime`, `chunkMethod` (`chapter_exact` | `fixed_window`), `chunkNumber`, `totalChunks`, `chunkTokens`

**Watermark**: `.last_upload_videos_chapters_{database}`, filters on `transcript_segments.created_at`.

**Deterministic UUIDs**: `uuid5(NAMESPACE_DNS, "chapter-video-{videoId}-ch{chapterNumber}-{chunkNumber}")` — handles duplicate playlist entries via dedup.

### Wayback Machine

Anti-empire.com WordPress archive is in `~/anti-empire/` with its own `README.md` documenting the `waybackup` tool usage, two-domain split (HTML vs wp-content), and monitoring.

## Database

PostgreSQL database: `substack`

### Schema: `substack`

Key tables: `publications`, `authors`, `posts`, `comments`, `tags`, `post_links`, `post_media`, `transcript_lines`, `load_status`

- `posts.content_html` — original HTML content
- `posts.content_text` — plain text with full-text search index
- `load_status` — tracks which files have been loaded (used by `load_posts.py --resume`)
- `post_media` — tracks images/audio URLs and download status

### Schema: `youtube` (podcasts database)

YouTube video data. Key tables: `playlists`, `playlist_videos`, `video_transcripts`, `transcript_segments`, `video_chapters`

- `video_transcripts` — video metadata + full transcript text (used by `upload_videos.py`)
- `transcript_segments` — timestamped transcript segments `(video_id, segment_index, start_seconds, end_seconds, text)`
- `video_chapters` — chapter markers `(video_id, position, start_seconds, title)`, populated by `upload_videos_chapters.py` from video descriptions

### Schema: `external`

Scraped articles from non-Substack sources cited in posts. Schema in `external_sources/create_external_tables.sql`.

Key tables: `sources`, `articles`, `comments`

- `articles.source_article_id` — original ID from source site (e.g. TopWar numeric ID)
- `articles.file_path` — local scrape file path, used for dedup (`UNIQUE(source_id, file_path)`)
- `articles.url` — canonical URL, also unique per source (`UNIQUE(source_id, url)`)
- `articles.updated_at` — set on upsert, used as watermark for incremental Weaviate uploads
- `comments.content_hash` — generated column `md5(username || body)`, used for upsert dedup (`UNIQUE(article_id, content_hash)`)
- `comments.updated_at` — set on upsert, used as watermark for incremental Weaviate uploads
- Sources: topwar.ru, livejournal.com, katyusha.org, wsj.com, thenation.com, topcor.ru, washingtonpost.com, liberium.ru, versia.ru, paulcraigroberts.org, arcaluinoe.info
- Views: `articles_with_source` (joins source info), `substack_citations` (cross-references with `substack.post_links`)

## Canonical Folder Naming

Always use the full Substack subdomain for folder names: `posts/{subdomain}/`. Known legacy aliases that have been migrated:
- `posts/drl/` -> `posts/drlivci/`
- `posts/slc/` -> `posts/slavlandchronicles/`
- `posts/esq/` -> `posts/edwardslavsquat/`

## Ingestion Runbook

See `INGEST_NEW_BLOG.md` for step-by-step commands (initial ingest + incremental refresh).

## Conventions

- Fetching from Substack API: always use `--delay 30 --jitter 4` to avoid rate limiting
- Paid content requires `--cookies cookies.json`
- Post list files are archived with timestamp suffix before updates: `{blog}_posts.{YYYYMMDD_HHMMSS}.json`
- Weaviate collection: `SubstackPostEngRu` (English and Russian content)

## Vector Databases

**Weaviate (primary)** — standalone Docker instance on port 8080/50051. Docker config in `weaviate/docker/`. API keys in `weaviate/docker/.env` (not committed; see `.env.example`). Config auto-loaded by `weaviate/config.py`.

Collections:
- `SubstackPostEngRu` — post chunks, OpenAI `text-embedding-3-small`, Cohere reranker
- `SubstackCommentEngRu` — individual comments, OpenAI embeddings
- `VideoChunkEngRu` — YouTube transcript chunks, JinaAI v3 `jina-embeddings-v3` (1024 dim), JinaAI reranker
- `VideoChunkSC` — YouTube transcript chunks for surgical_compass database, JinaAI v3
- `VideoChapterChunkPodcasts` — chapter-aware YouTube transcript chunks for podcasts database, JinaAI v3 (see below)
- `ExternalArticleEngRu` — external article chunks, JinaAI v3, watermark-based incremental uploads
- `ExternalCommentEngRu` — external comment bundles (grouped by article), JinaAI v3

Upload scripts: `weaviate/upload_posts.py`, `weaviate/upload_comments.py`, `weaviate/upload_videos.py`, `weaviate/upload_videos_chapters.py`, `weaviate/upload_external.py`

**Weaviate Embedded (experimental)** — `weaviate/embedded.py` runs Weaviate in-process with Jina AI embeddings (`jina-embeddings-v3`, 1024 dims). Activate with `WEAVIATE_EMBEDDED=1 JINAAI_API_KEY=... python weaviate/embedded.py`. Data persists to `~/.local/share/weaviate-embedded/`.

**LanceDB (experimental)** — `embedded_vectordb/lancedb/`. Fully embedded (no server), uses Jina AI embeddings. Supports vector, FTS, and hybrid search. Data persists to `~/.local/share/lancedb-substack/`.

**ChromaDB (experimental)** — `embedded_vectordb/chromadb/`. Persistent embedded client with Jina AI embeddings. Required a manual patch for Python 3.14 compatibility. Data persists to `~/.local/share/chromadb-substack/`.

See `embedded_vectordb/README.md` for full comparison and usage.

**MotherDuck (experimental)** — SaaS DuckDB with built-in `embedding()` (OpenAI `text-embedding-3-small`, 512 dim) and `array_cosine_similarity()`. Auth via `MOTHERDUCK_TOKEN` env var. Table: `substack_posts` in `my_db`.

```bash
python motherduck/upload_posts.py                           # incremental (new posts only)
python motherduck/upload_posts.py --database substack       # substack DB only
python motherduck/upload_posts.py --database podcasts       # podcasts DB only
python motherduck/upload_posts.py --publication edwardslavsquat  # one publication
python motherduck/upload_posts.py --full                    # drop and recreate (full reload)
python motherduck/upload_posts.py --skip-embeddings         # insert only, no embedding()
python motherduck/upload_posts.py --embed-only              # only run embedding update
```

Tables: `substack_posts` (chunked post content), `substack_comments` (individual comments), `youtube_transcript_chunks` (fixed-window transcript chunks). Default is incremental for posts/comments (dedup by post_id/comment_id, embed only new rows). Use `--full` to drop and recreate. Upload scripts: `motherduck/upload_posts.py`, `motherduck/upload_comments.py`, `motherduck/upload_video_chapters.py`, `motherduck/upload_video_transcripts.py`.

```bash
python motherduck/upload_comments.py                           # incremental (new comments only)
python motherduck/upload_comments.py --database substack       # substack DB only
python motherduck/upload_comments.py --publication martyrmade  # one publication
python motherduck/upload_comments.py --full                    # drop and recreate (full reload)
python motherduck/upload_comments.py --skip-embeddings         # insert only
python motherduck/upload_comments.py --embed-only              # only run embedding update
```

YouTube video chapters from the `podcasts` PostgreSQL database: `motherduck/upload_video_chapters.py`. Two tables for catalog browsing and semantic search:
- `youtube_videos` — one row per video (281 rows), description embedded
- `youtube_chapters` — one row per chapter (3,384 rows), transcript embedded. Videos without chapters get a single "Full Transcript" row.

```bash
python motherduck/upload_video_chapters.py                     # full reload (default: podcasts DB)
python motherduck/upload_video_chapters.py --skip-embeddings   # insert only
python motherduck/upload_video_chapters.py --embed-only        # only run embeddings
```

Fixed-window YouTube transcript chunks from `podcasts` database: `motherduck/upload_video_transcripts.py`. Mirrors `weaviate/upload_videos.py` — chunks full transcripts with 1000-token windows, 250-token overlap. Table: `youtube_transcript_chunks` (~19,700 chunks from ~834 videos). Full reload each run.

```bash
python motherduck/upload_video_transcripts.py                         # full reload (default: podcasts DB)
python motherduck/upload_video_transcripts.py --database podcasts     # explicit DB
python motherduck/upload_video_transcripts.py --skip-embeddings       # insert only
python motherduck/upload_video_transcripts.py --embed-only            # only run embeddings
```

Posts reuse chunking logic from `weaviate/upload_posts.py` (CHUNK_SIZE=700, OVERLAP=150). All tables have FTS indexes (BM25) and vector embeddings (OpenAI `text-embedding-3-small`, 512 dim). FTS keys must be integer-castable (`BIGINT`) for `match_bm25()`. Semantic search example:
```sql
SELECT title, subdomain, array_cosine_similarity(embedding('search query'), content_embedding) AS score
FROM substack_posts ORDER BY score DESC LIMIT 10;
```
