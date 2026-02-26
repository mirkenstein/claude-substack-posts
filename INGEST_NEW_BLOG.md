# Ingest a New Substack Blog

## Setup

```bash
export BLOG=martyrmade   # subdomain from https://<subdomain>.substack.com
```

---

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
```

### Step 4: Upload to Weaviate

```bash
python weaviate/upload_posts.py --publication ${BLOG}
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

The script archives the old post list as `{blog}_posts.{timestamp}.json` before updating.

---

## Refresh Comments on Recent Posts

Re-fetch the last N posts to pick up new comments, then reload into Postgres and Weaviate:

```bash
# 1. Re-fetch last 5 posts (overwrites JSON files with fresh comments)
python main.py --from-list posts/${BLOG}/${BLOG}_posts.json \
    --output-dir posts/${BLOG}/ --last 5 --delay 30 --jitter 4 -v

# 2. Reload those 5 into Postgres (--last picks by mtime, bypasses --resume)
python load_posts.py --dir posts/${BLOG}/ --resume --last 5 -v

# 3. Incremental Weaviate upload (watermark picks up the refreshed posts)
python weaviate/upload_posts.py
```

Notes:
- `--last N` in `main.py` takes the first N posts from the list (most recent first)
- `--last N` in `load_posts.py` selects files by modification time and forces reload even with `--resume`
- Step 3 uses the watermark (no `--publication` flag) so only the refreshed posts get re-uploaded
- Do **not** use `--resume` in step 1 — you want to overwrite the existing files with fresh data
