# Ingest a New Substack Blog

## Setup
https://huabinoliver.substack.com/
```bash
export BLOG=eventsinukraine   # subdomain from https://<subdomain>.substack.com
```
```shell
for BLOG in "eventsinukraine" "drlivci" "kamilkazani" "slavlandchronicles" "huabinoliver" "iurierosca";
do python refresh_blog.py $BLOG --cookies cookies.json; 
done
```


## Initial Ingest (first time)

### Step 1: List all posts

```bash
python main.py --newsletter https://${BLOG}.substack.com --list --all --output-dir posts/${BLOG}/ -v
```

For paid content, add `--cookies cookies.json`.

### Step 2: Fetch full content + comments

```bash
python main.py --from-list posts/${BLOG}/${BLOG}_posts.json --output-dir posts/${BLOG}/ --resume --delay 30 --jitter 4 -v
```

Re-run with `--resume` to pick up where you left off if interrupted.

### Step 3: Load into PostgreSQL

```bash
python load_posts.py --dir posts/${BLOG}/ --resume --log etl_${BLOG}.log -v
# For non-default database:
# python load_posts.py --dir posts/${BLOG}/ --resume --log etl_${BLOG}.log --database podcasts -v
```

### Step 4: Upload to Weaviate and ChromaDB

```bash
# Weaviate (add --database podcasts for non-default DB)
python weaviate/upload_posts.py --publication ${BLOG}
python weaviate/upload_comments.py --publication ${BLOG}

# ChromaDB (requires JINA_API_KEY for client-side embeddings)
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_posts.py --publication ${BLOG}
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_comments.py --publication ${BLOG}
```

### Step 5: Download and analyze images

```bash
# Extract media URLs from posts and download images
python download_media.py --extract --download --type image --publication ${BLOG}
python download_media.py --download --type cover_image --publication ${BLOG}

# Pass 1: fast triage with local Ollama (gemma3:27b) — runs ~3.5 sec/image
# Use nohup for large publications; PYTHONUNBUFFERED=1 ensures log is written in real time
PYTHONUNBUFFERED=1 nohup python analyze_media.py --publication ${BLOG} > analyze_${BLOG}.log 2>&1 &
tail -f analyze_${BLOG}.log

# Pass 2: deep analysis on critical images (screenshot/tweet/chart/document/table/infographic/map)
python analyze_media.py --pass2 --publication ${BLOG} --api-key 'sk-ant-...'
```

The default model for pass 1 is `ollama/gemma3:27b`. You can use `--model ollama/qwen2.5vl:32b` or any other Ollama vision model. Pass 2 defaults to Anthropic Haiku.

Analysis results are saved both to the DB (`post_media` columns) and as JSON files alongside each image (`{image}.analysis.json`).

### Step 6: Download audio and transcribe podcasts

```bash
# Download podcast audio
python download_media.py --extract --download --type audio --publication ${BLOG}

# Preview what will be transcribed (dry run)
./transcribe_all.sh --subdomain ${BLOG} --dry-run

# Transcribe all episodes (speaker counts inferred from DB heuristics)
./transcribe_all.sh --subdomain ${BLOG}

# For martyrmade (uses separate database):
./transcribe_all.sh --subdomain martyrmade --database podcasts

# Override speaker count for all episodes if needed:
./transcribe_all.sh --subdomain ${BLOG} --num-speakers 2
```

Requires `HUGGING_FACE_HUB_TOKEN` set in your environment. See `transcribe/SETUP.md` for one-time setup (pyannote license acceptance, dependencies).

### Step 7: Ingest transcripts into Postgres and Weaviate

```bash
# Ingest all transcribed episodes
python ingest_transcripts.py posts/saved/audio/${BLOG}/

# Or a single episode
python ingest_transcripts.py posts/saved/audio/${BLOG}/<post_id>

# Skip Weaviate upload
python ingest_transcripts.py posts/saved/audio/${BLOG}/ --skip-weaviate

# Dry run
python ingest_transcripts.py posts/saved/audio/${BLOG}/ --dry-run
```

This stores speaker turns in `transcript_lines`, updates `posts.content_html` with the full transcript, and uploads chunked text to Weaviate.

### Verify

```bash
# Add database='podcasts' to DatabaseConnection() for non-default DB
python3 -c "
from src.db.connection import DatabaseConnection
db = DatabaseConnection()
conn = db.connect()
cur = conn.cursor()
cur.execute('''
    SELECT count(*), sum(comment_count), sum(wordcount)
    FROM substack.posts p
    JOIN substack.publications pub ON pub.id = p.publication_id
    WHERE pub.subdomain = '${BLOG}'
''')
posts, comments, words = cur.fetchone()
print(f'Posts: {posts}, Comments: {comments}, Words: {words}')
cur.close()
db.close()
"
```

---

## Incremental Refresh (subsequent runs)

Check for new posts and ingest only what's new:

```bash
python refresh_blog.py ${BLOG}

# For blogs in the podcasts database:
python refresh_blog.py ${BLOG} --database podcasts --cookies cookies.json
```

Check more than the default 10 latest posts:

```bash
python refresh_blog.py ${BLOG} --check 20
```

Dry run (show new posts without ingesting):

```bash
python refresh_blog.py ${BLOG} --dry-run
```

Skip Weaviate upload:

```bash
python refresh_blog.py ${BLOG} --skip-weaviate
```

With custom delay between fetches:

```bash
python refresh_blog.py ${BLOG} --delay 30 --jitter 4
```

For paid content:

```bash
python refresh_blog.py ${BLOG} --cookies cookies.json
```

The `--database` flag threads through to both PostgreSQL loading and Weaviate upload.

The script archives the old post list as `{blog}_posts.{timestamp}.json` before updating.

After refresh, upload to ChromaDB (refresh_blog.py only handles Weaviate):

```bash
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_posts.py
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_comments.py
```

Download and analyze any new media from the new posts:

```bash
python download_media.py --extract --download --type image --publication ${BLOG}
python download_media.py --download --type cover_image --publication ${BLOG}
python download_media.py --download --type audio --publication ${BLOG}

# Analyze new images (only unanalyzed images are processed)
python analyze_media.py --publication ${BLOG}
python analyze_media.py --pass2 --publication ${BLOG} --api-key 'sk-ant-...'
```

---

## Refresh Comments on Recent Posts

Re-fetch the last N posts to pick up new comments and media, then reload into Postgres and Weaviate:

```bash
# 1. Re-fetch last 5 posts (overwrites JSON files with fresh comments)
python main.py --from-list posts/${BLOG}/${BLOG}_posts.json \
    --output-dir posts/${BLOG}/ --last 5 --delay 30 --jitter 4 --cookies cookies.json -v

# 2. Reload those 5 into Postgres (--last picks by mtime, bypasses --resume)
#    Add --database podcasts for non-default DB
python load_posts.py --dir posts/${BLOG}/ --resume --last 5 -v

# 3. Extract and download any new media from refreshed posts
python download_media.py --extract --download --type image --publication ${BLOG}
python download_media.py --download --type cover_image --publication ${BLOG}
python download_media.py --download --type audio --publication ${BLOG}

# 4. Analyze new images (only unanalyzed images are processed)
python analyze_media.py --publication ${BLOG}
python analyze_media.py --pass2 --publication ${BLOG} --api-key 'sk-ant-...'

# 5. Incremental Weaviate + ChromaDB upload (watermark picks up the refreshed posts and comments)
python weaviate/upload_posts.py
python weaviate/upload_comments.py
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_posts.py
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_comments.py
```

Notes:
- Always use `--cookies cookies.json` when fetching — paid posts will be truncated without it
- `--last N` in `main.py` takes the first N posts from the list (most recent first)
- `--last N` in `load_posts.py` selects files by modification time and forces reload even with `--resume`
- Step 3 re-extracts media URLs from post HTML and downloads any that are new (already-downloaded files are skipped)
- Step 4 uses the watermark (no `--publication` flag) so only the refreshed posts get re-uploaded
- Do **not** use `--resume` in step 1 — you want to overwrite the existing files with fresh data

---

## External Sources (non-Substack articles)

Load scraped articles from external sites (TopWar, LiveJournal, WSJ, Unlimited Hangout, etc.) into the `external` schema and Weaviate.

### Load into PostgreSQL

```bash
# Load all JSON files from scraped_data/
python external_sources/load_external.py

# Load a single file
python external_sources/load_external.py --file topcor_articles.json

# Load from subdirectory
python external_sources/load_external.py --file new_cumulative/topwar_articles.json

# Load into a different database (e.g. podcasts)
python external_sources/load_external.py --database podcasts

# Drop & recreate schema, then load all
python external_sources/load_external.py --recreate

# Just recreate schema (no data load)
python external_sources/load_external.py --recreate --no-load
```

Source domain is inferred from filename prefix (e.g. `topwar_*` → `topwar.ru`). New sources are auto-created. JSON files go in `external_sources/scraped_data/`.

### Upload to Weaviate

```bash
# Incremental upload (new/updated since last run)
python weaviate/upload_external.py

# Re-upload everything
python weaviate/upload_external.py --all

# Upload records updated after a date
python weaviate/upload_external.py --since 2025-01-01

# Only articles or only comments
python weaviate/upload_external.py --articles-only
python weaviate/upload_external.py --comments-only

# Upload to podcasts collections (ExternalArticlePodcasts / ExternalCommentPodcasts)
python weaviate/upload_external.py --database podcasts
```

### First-time setup for a new database

```bash
# 1. Create the external schema in the target database
python external_sources/load_external.py --database podcasts --recreate --no-load

# 2. Load articles
python external_sources/load_external.py --database podcasts

# 3. Create Weaviate collections and upload
python weaviate/upload_external.py --database podcasts
```
