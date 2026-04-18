# Ingest a Single Post from a New Blog

For one-off posts from blogs not in the regular pipeline. No full newsletter ingest needed.

## Quick Start

```bash
# Single post (database auto-detected from substacks.json, or defaults to 'substack')
python ingest_single_post.py https://movingtorussia.substack.com/p/lenin-inspired-by-vendee-french-revolution

# Explicit database
python ingest_single_post.py https://escapekey.substack.com/p/some-post --database podcasts

# Paid content
python ingest_single_post.py https://blog.substack.com/p/paid-post --cookies cookies.json

# Multiple posts (same or different blogs)
python ingest_single_post.py \
    https://pikulexpedition.substack.com/p/trump-timeline-of-an-israeli-asset \
    https://goldennuggiez.substack.com/p/mao-zedong-skull-and-bones-and-the

# Preview without executing
python ingest_single_post.py https://blog.substack.com/p/post --dry-run

# Skip Weaviate or media
python ingest_single_post.py https://blog.substack.com/p/post --skip-weaviate
python ingest_single_post.py https://blog.substack.com/p/post --skip-media
```

The script handles the full pipeline: fetch post, load into PostgreSQL, download media, upload to Weaviate. The subdomain and slug are extracted from the URL automatically.

## What it does

1. **Fetch** the post from the Substack API (`main.py --url`)
2. **Load** into PostgreSQL (`load_posts.py --file`)
3. **Download** images and audio (`download_media.py --extract --download`)
4. **Upload** to Weaviate (`upload_posts.py --publication`)

Database is auto-detected from `substacks.json` when the blog is listed there. For unknown blogs, defaults to `substack` (or use `--database` to override).

## Optional follow-up steps

These are not handled by `ingest_single_post.py` and must be run manually if needed.

### Transcribe podcast audio

```bash
export BLOG=escapekey

# Preview
./transcribe_all.sh --subdomain ${BLOG} --dry-run

# Transcribe (override speaker count if needed)
./transcribe_all.sh --subdomain ${BLOG} --num-speakers 2

# Ingest transcript into Postgres + Weaviate
python ingest_transcripts.py posts/saved/audio/${BLOG}/
```

### Analyze images

```bash
# Pass 1: local Ollama triage
python analyze_media.py --publication ${BLOG}

# Pass 2: deep analysis on critical images
python analyze_media.py --pass2 --publication ${BLOG} --api-key 'sk-ant-...'
```

## Manual step-by-step (if not using the script)

```bash
export URL=https://movingtorussia.substack.com/p/lenin-inspired-by-vendee-french-revolution
export BLOG=movingtorussia
export SLUG=lenin-inspired-by-vendee-french-revolution

# 1. Fetch
mkdir -p posts/${BLOG}/
python main.py --url ${URL} --output posts/${BLOG}/${SLUG}.json --verbose

# 2. Load into PostgreSQL (database auto-detected)
python load_posts.py --file posts/${BLOG}/${SLUG}.json --verbose

# 3. Download media
python download_media.py --extract --download --publication ${BLOG}

# 4. Upload to Weaviate (database auto-detected)
python weaviate/upload_posts.py --publication ${BLOG}
```

## Files created

| Asset | Location |
|-------|----------|
| Post JSON | `posts/{blog}/{slug}.json` |
| Images | `posts/saved/images/{blog}/{post_id}/` |
| Cover images | `posts/saved/covers/{blog}/{post_id}/` |
| Podcast audio | `posts/saved/audio/{blog}/{post_id}/episode.mp3` |
| Image analysis | `posts/saved/images/{blog}/{post_id}/{image}.analysis.json` |
| Transcripts | `posts/saved/audio/{blog}/{post_id}/transcript.json` |
