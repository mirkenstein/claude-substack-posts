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
weaviate/                           # Weaviate upload scripts
twitter/                            # Twitter data ingestion (separate pipeline)
src/                                # Core library code
src/db/                             # Database connection and loader
```

## Key Scripts and Their Roles

### Ingestion Pipeline

| Script | Purpose |
|--------|---------|
| `main.py` | Fetch post lists (`--list --all`) and full post content (`--from-list`) from Substack API |
| `load_posts.py` | Load post JSONs into PostgreSQL. Supports `--resume`, `--log`, structured error recording in `load_status` table |
| `weaviate/upload_posts.py` | Upload posts to Weaviate. Watermark-based incremental uploads (`--publication`) |
| `refresh_blog.py` | End-to-end incremental refresh: diff, fetch, load, upload. Convenience wrapper |
| `download_media.py` | Extract and download images/audio from posts |
| `ingest_transcripts.py` | Load podcast transcripts into `transcript_lines` table and Weaviate |

### load_posts.py vs refresh_blog.py postgres loading

Both load posts into PostgreSQL using `PostLoader`, but `load_posts.py` is the canonical loader:

- **`load_posts.py`**: Full-featured ETL with `--resume` (checks `load_status` table), structured logging to file (`--log`), error recording in DB, graceful JSON read error handling. Use for bulk loads and recovery.
- **`refresh_blog.py`**: Has an inline `load_into_postgres()` that works for the happy path (1-2 new posts) but lacks resume, file logging, and error recording in DB. It calls `weaviate/upload_posts.py` externally but reimplements the postgres loading inline.

When in doubt, prefer `load_posts.py --resume` for postgres loading.

### Transcription

| Script | Purpose |
|--------|---------|
| `transcribe_all.sh` | Batch transcription with per-episode speaker count inference from DB heuristics |
| `transcribe/transcribe_interview.py` | Single episode transcription using whisperx |

Speaker count heuristics in `transcribe_all.sh`:
- **anti-empire**: Always 3 speakers (interview format)
- **edwardslavsquat**: Checks description for multiple names, "w/", "conversation with", "interview with" patterns
- **slavlandchronicles**: Checks title for `w/` or `W/` pattern (case-insensitive)
- Default: 1 speaker (solo podcast)

### External Sources

`external_sources/load_external.py` loads scraped articles from non-Substack sites (TopWar, LiveJournal, Katyusha, WSJ, Washington Post, TopCor, The Nation) into the `external` schema.

```bash
python external_sources/load_external.py                          # load all JSON files from scraped_data/
python external_sources/load_external.py --file topcor_articles.json  # load one file
python external_sources/load_external.py --recreate               # drop & recreate schema, then load all
python external_sources/load_external.py --recreate --no-load     # just recreate schema
```

- Schema defined in `external_sources/create_external_tables.sql`
- Source domain is inferred from filename prefix via `FILENAME_DOMAIN_MAP` (e.g. `topwar_*` -> `topwar.ru`, `WSJ_*` -> `wsj.com`)
- New sources are auto-created if not in the seed list
- Duplicate URLs across files (e.g. same topwar article in `topwar_articles.json` and `topwar_other_articles.json`) are merged silently
- JSON files go in `external_sources/scraped_data/`
- `--recreate` runs `DROP SCHEMA external CASCADE` then re-runs the SQL file — use when schema changes

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

### Schema: `external`

Scraped articles from non-Substack sources cited in posts. Schema in `external_sources/create_external_tables.sql`.

Key tables: `sources`, `articles`, `comments`

- `articles.source_article_id` — original ID from source site (e.g. TopWar numeric ID)
- `articles.file_path` — local scrape file path, used for dedup (`UNIQUE(source_id, file_path)`)
- `articles.url` — canonical URL, also unique per source (`UNIQUE(source_id, url)`)
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
