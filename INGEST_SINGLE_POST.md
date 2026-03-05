# Ingest a Single Post from a New Blog

For one-off posts from blogs not in the regular pipeline. No full newsletter ingest needed.

## Quick Reference

```bash
export URL=https://andrewsullivan.substack.com/p/edward-luttwak-on-putin-china-brexit
export BLOG=andrewsullivan   # subdomain from the URL
export SLUG=edward-luttwak-on-putin-china-brexit
```

## Step 1: Fetch the post

```bash
mkdir -p posts/${BLOG}/
python main.py --url ${URL} --output posts/${BLOG}/${SLUG}.json --verbose

# For paid content:
python main.py --url ${URL} --output posts/${BLOG}/${SLUG}.json --cookies cookies.json --verbose
```

## Step 2: Load into PostgreSQL

```bash
python load_posts.py --file posts/${BLOG}/${SLUG}.json --verbose

# For non-default database:
python load_posts.py --file posts/${BLOG}/${SLUG}.json --database podcasts --verbose
```

## Step 3: Fetch podcast URLs (if the post is a podcast)

This must happen **before** media extraction — `--extract` reads `podcast_url` from the DB to create audio rows.

```bash
python download_media.py --fetch-podcasts --publication ${BLOG}
```

Skip this step if the post is not a podcast.

## Step 4: Download images and audio

```bash
# Extract media URLs from post HTML + podcast_url, then download
python download_media.py --extract --download --publication ${BLOG}
```

To download only specific types:

```bash
python download_media.py --extract --publication ${BLOG}
python download_media.py --download --type image --publication ${BLOG}
python download_media.py --download --type cover_image --publication ${BLOG}
python download_media.py --download --type audio --publication ${BLOG}
```

## Step 5: Upload to Weaviate

```bash
python weaviate/upload_posts.py --publication ${BLOG}

# For non-default database:
python weaviate/upload_posts.py --publication ${BLOG} --database podcasts
```

## Step 6 (optional): Transcribe podcast

```bash
# Preview
./transcribe_all.sh --subdomain ${BLOG} --dry-run

# Transcribe (override speaker count if needed)
./transcribe_all.sh --subdomain ${BLOG} --num-speakers 2

# Ingest transcript into Postgres + Weaviate
python ingest_transcripts.py posts/saved/audio/${BLOG}/

# For ChromaDB (not handled by ingest_transcripts.py):
JINA_API_KEY=... python embedded_vectordb/chromadb/upload_posts.py --publication ${BLOG}
```

`ingest_transcripts.py` handles everything in one shot: stores speaker turns in `transcript_lines`, updates `posts.content_html`, and uploads chunked transcript to Weaviate. No need to re-run `upload_posts.py` — the transcript ingestion already covers the Weaviate upload. ChromaDB requires a separate upload step.

## Step 7 (optional): Analyze images

```bash
# Pass 1: local Ollama triage
python analyze_media.py --publication ${BLOG}

# Pass 2: deep analysis on critical images
python analyze_media.py --pass2 --publication ${BLOG} --api-key 'sk-ant-...'
```

## Multiple posts from the same blog

Repeat step 1 for each post, then run steps 2-5 once with `--publication`:

```bash
python main.py --url https://${BLOG}.substack.com/p/first-post --output posts/${BLOG}/first-post.json
python main.py --url https://${BLOG}.substack.com/p/second-post --output posts/${BLOG}/second-post.json

python load_posts.py --dir posts/${BLOG}/ --resume --verbose
python download_media.py --fetch-podcasts --publication ${BLOG}
python download_media.py --extract --download --publication ${BLOG}
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
